import json
import os
import traceback
from typing import Dict, List, Optional

import requests
from azure.storage.queue import QueueClient
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from utils.logging import logger
from services.cosmos_db import get_shop, update_shop_onedrive_info


router = APIRouter()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
PAGE_SIZE = 200
QUEUE_BATCH_SIZE = 10
QUEUE_NAME = "bybaek-photo-sync"


# ──────────────────────────────────────────
# 요청 / 응답 모델
# ──────────────────────────────────────────

class SyncPhotosRequest(BaseModel):
    root_folder_item_id: str = "root"


class SyncPhotosResponse(BaseModel):
    success: bool
    queued: int
    batches: int
    message: str


# ──────────────────────────────────────────
# Graph API 헬퍼
# ──────────────────────────────────────────

def graph_get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, params=params, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Graph GET failed: {response.status_code} {response.text[:200]}")
    return response.json()


def get_user_drive_id(token: str) -> str:
    data = graph_get(f"{GRAPH_BASE}/me/drive", token, params={"$select": "id"})
    logger.info(f"[onedrive] drive_id: {data['id']}")
    return data["id"]


def is_photo(item: Dict) -> bool:
    if "file" not in item:
        return False
    ext = os.path.splitext(item.get("name", ""))[1].lower()
    if ext in PHOTO_EXTENSIONS:
        return True
    return item.get("file", {}).get("mimeType", "").startswith("image/")


def sanitize_blob_path(path: str) -> str:
    return path.strip("/").replace("\\", "/")


# ──────────────────────────────────────────
# Delta API
# ──────────────────────────────────────────

def collect_delta_photos(token: str, drive_id: str, delta_link: Optional[str]) -> tuple:
    """
    Delta API로 변경된 사진 목록만 수집.
    Returns: (photos: list[dict], next_delta_link: str)
    """
    url = delta_link or f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
    if delta_link:
        logger.info("[onedrive] Delta 동기화 시작 (변경분만)")
    else:
        logger.info("[onedrive] 전체 동기화 시작 (첫 로그인)")

    photos = []
    next_delta_link = None
    params = {
        "$top": PAGE_SIZE,
        "$select": "id,name,folder,file,parentReference,lastModifiedDateTime,deleted"
    }

    while url:
        data = graph_get(url, token, params=params)
        params = None

        for item in data.get("value", []):
            if item.get("deleted"):
                continue
            if is_photo(item):
                photos.append(item)

        url = data.get("@odata.nextLink")
        if not url:
            next_delta_link = data.get("@odata.deltaLink")

    logger.info(f"[onedrive] Delta 결과 → 변경된 사진 {len(photos)}장")
    return photos, next_delta_link


# ──────────────────────────────────────────
# Azure Queue 헬퍼
# ──────────────────────────────────────────

def get_queue_client() -> QueueClient:
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    client = QueueClient.from_connection_string(
        connection_string,
        queue_name=QUEUE_NAME
    )
    try:
        client.create_queue()
        logger.info(f"[queue] 큐 생성 완료: {QUEUE_NAME}")
    except Exception:
        pass
    return client


def enqueue_photo_batches(
    queue_client: QueueClient,
    photos: List[Dict],
    shop_id: str,
    drive_id: str,
    container_name: str,
) -> int:
    """
    사진 목록을 QUEUE_BATCH_SIZE씩 묶어 큐에 등록.
    ⚠️ token은 큐에 저장하지 않음 - worker가 DB에서 refresh_token으로 직접 발급
    Returns: 등록된 배치 수
    """
    batches = 0
    for i in range(0, len(photos), QUEUE_BATCH_SIZE):
        batch = photos[i : i + QUEUE_BATCH_SIZE]

        message_items = []
        for photo in batch:
            parent_path = photo.get("parentReference", {}).get("path", "")
            if "root:" in parent_path:
                parent_path = parent_path.split("root:")[-1]
            name = photo["name"]
            relative_path = sanitize_blob_path(
                f"{parent_path}/{name}" if parent_path else name
            )
            message_items.append({
                "item_id": photo["id"],
                "name": name,
                "relative_path": relative_path,
                "mime_type": photo.get("file", {}).get("mimeType", ""),
                "last_modified": photo.get("lastModifiedDateTime", ""),
            })

        message = json.dumps({
            "shop_id": shop_id,
            "drive_id": drive_id,
            "container_name": container_name,
            "photos": message_items,
            # token 없음 - worker가 refresh_token으로 직접 발급
        })
        queue_client.send_message(message)
        batches += 1

    return batches


# ──────────────────────────────────────────
# 메인 엔드포인트
# ──────────────────────────────────────────

@router.post("/sync-photos", response_model=SyncPhotosResponse)
def sync_onedrive_photos(req: SyncPhotosRequest, request: Request) -> SyncPhotosResponse:
    """
    OneDrive 동기화 엔드포인트.
    변경된 사진 목록을 수집해 큐에 등록하고 즉시 응답.
    실제 업로드/필터링은 photo_queue_worker.py가 처리.
    """
    try:
        access_token = request.headers.get("x-ms-token-aad-access-token")
        if not access_token:
            raise HTTPException(status_code=401, detail="MS 로그인 필요.")

        shop_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "unknown")
        container_name = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")

        logger.info(f"[onedrive] 동기화 시작 → shop_id={shop_id}")

        drive_id = get_user_drive_id(access_token)

        # DB에서 delta_link 조회 (지연님 연동 완료)
        shop_info = get_shop(shop_id)
        delta_link = shop_info.get("one_delta_link") if shop_info else None

        photos, next_delta_link = collect_delta_photos(access_token, drive_id, delta_link)

        if not photos:
            return SyncPhotosResponse(
                success=True, queued=0, batches=0,
                message="변경된 사진이 없습니다."
            )

        queue_client = get_queue_client()
        batches = enqueue_photo_batches(
            queue_client, photos, shop_id, drive_id, container_name
        )

        if next_delta_link:
            try:
                update_shop_onedrive_info(shop_id, {"one_delta_link": next_delta_link})
                logger.info("[onedrive] Delta Link 저장 완료")
            except Exception as e:
                logger.error(f"[onedrive] Delta Link 저장 실패: {e}")

        logger.info(f"[onedrive] 큐 등록 완료 → {len(photos)}장 / {batches}개 배치")

        return SyncPhotosResponse(
            success=True,
            queued=len(photos),
            batches=batches,
            message=f"{len(photos)}장을 큐에 등록했습니다. 백그라운드에서 처리됩니다."
        )

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))