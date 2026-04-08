"""
파일명: routers/onedrive.py
역할: OneDrive 사진 동기화 라우터

[핵심 설계]
1. Delta API: 마지막 동기화 이후 변경된 사진만 가져옴 (첫 로그인 시 전체 동기화)
2. DB 중복 체크: Cosmos DB에 이미 있는 사진은 업로드 스킵
3. 업로드 / 필터링 분리: 업로드 완료 즉시 응답 반환, 필터링은 BackgroundTasks로 비동기 처리

[흐름]
GET /me/drive/root/delta (delta_link 있으면 거기서부터, 없으면 전체)
→ DB 중복 체크 (이미 있는 사진 스킵)
→ Blob Storage 업로드
→ Cosmos DB Photo 저장
→ 즉시 응답 반환 (uploaded / skipped / failed)
→ BackgroundTasks: 필터링 실행 → Photos 화면에 is_usable=true 사진 자동 추가
"""

import asyncio
import mimetypes
import os
from typing import Dict, Generator, List, Optional

import requests
import traceback
import concurrent.futures
from urllib.parse import quote
from fastapi import APIRouter, BackgroundTasks, HTTPException, status, Request
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient, ContentSettings
from utils.logging import logger
from services.cosmos_db import save_photo, get_photo_by_id, update_shop_onedrive_info


router = APIRouter()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
INSTAGRAM_SUPPORTED = {".jpg", ".jpeg", ".png"}
PAGE_SIZE = 200


# ──────────────────────────────────────────
# 요청 / 응답 모델
# ──────────────────────────────────────────

class SyncPhotosResponse(BaseModel):
    success: bool
    uploaded: int       # 신규 업로드
    skipped: int        # DB 중복 스킵
    failed: int         # 업로드 실패
    filter_started: bool  # 백그라운드 필터링 시작 여부
    container: str


class SyncPhotosRequest(BaseModel):
    root_folder_item_id: str = "root"


# ──────────────────────────────────────────
# Graph API 헬퍼
# ──────────────────────────────────────────

def graph_get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, params=params, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Graph GET failed: {response.status_code} {response.text}")
    return response.json()


def get_user_drive_id(token: str) -> str:
    data = graph_get(f"{GRAPH_BASE}/me/drive", token, params={"$select": "id"})
    logger.info(f"[onedrive] drive_id: {data['id']}")
    return data["id"]


def stream_download_file(download_url: str, token: str) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(download_url, headers=headers, stream=True, timeout=300)
    if response.status_code >= 400:
        raise RuntimeError(f"Download failed: {response.status_code} {response.text[:100]}")
    return response


def is_photo(item: Dict) -> bool:
    if "file" not in item:
        return False
    name = item.get("name", "")
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXTENSIONS:
        return True
    return item.get("file", {}).get("mimeType", "").startswith("image/")


def sanitize_blob_path(path: str) -> str:
    return path.strip("/").replace("\\", "/")


# ──────────────────────────────────────────
# Delta API: 변경된 파일만 가져오기
# ──────────────────────────────────────────

def iter_delta_photos(token: str, drive_id: str, delta_link: Optional[str]) -> tuple:
    """
    Delta API로 변경된 사진만 가져옴.

    Args:
        delta_link: 이전 동기화 때 저장한 delta_link (없으면 전체 동기화)

    Returns:
        (photos: list[dict], next_delta_link: str)
        - photos: 신규/수정된 사진 목록
        - next_delta_link: 다음 동기화 때 사용할 delta_link (DB에 저장해야 함)
    """
    if delta_link:
        # 이전 동기화 이후 변경분만
        url = delta_link
        logger.info(f"[onedrive] Delta 동기화 시작 (변경분만)")
    else:
        # 첫 동기화: 전체
        url = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
        logger.info(f"[onedrive] 전체 동기화 시작 (첫 로그인)")

    photos = []
    next_delta_link = None
    params = {
        "$top": PAGE_SIZE,
        "$select": "id,name,folder,file,parentReference,lastModifiedDateTime,deleted"
    }

    while url:
        data = graph_get(url, token, params=params)
        params = None  # 첫 요청 이후엔 파라미터 제거 (nextLink에 이미 포함됨)

        for item in data.get("value", []):
            # 삭제된 항목 스킵
            if item.get("deleted"):
                continue
            if is_photo(item):
                photos.append(item)

        # 다음 페이지
        url = data.get("@odata.nextLink")

        # 마지막 페이지에 delta_link 포함
        if not url:
            next_delta_link = data.get("@odata.deltaLink")

    logger.info(f"[onedrive] Delta 결과 → 변경된 사진 {len(photos)}장")
    return photos, next_delta_link


# ──────────────────────────────────────────
# 메인 엔드포인트
# ──────────────────────────────────────────

@router.post(
    "/sync-photos",
    response_model=SyncPhotosResponse,
    status_code=status.HTTP_200_OK,
)
def sync_onedrive_photos(
    req: SyncPhotosRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> SyncPhotosResponse:
    """
    OneDrive 사진 동기화 엔드포인트.

    1. Delta API로 신규/변경 사진만 가져옴
    2. DB 중복 체크 후 신규만 Blob 업로드
    3. 즉시 응답 반환
    4. 백그라운드에서 필터링 실행
    """
    try:
        token = request.headers.get("x-ms-token-aad-access-token")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MS 로그인 필요. x-ms-token-aad-access-token 헤더 없음."
            )

        shop_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "unknown")
        logger.info(f"[onedrive] 동기화 시작 → shop_id={shop_id}")

        # ── Blob Storage 클라이언트 ──
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client(container=container_name)

        # ── Drive ID + Delta Link 조회 ──
        drive_id = get_user_drive_id(token)

        # TODO: from services.cosmos_db import get_auth
        # shop_info = get_auth(shop_id)
        # delta_link = shop_info.get("one_delta_link") if shop_info else None
        delta_link = None  # 목업: 항상 전체 동기화 (실제 연동 시 위 코드 사용)

        # ── Delta API로 변경된 사진만 가져오기 ──
        changed_photos, next_delta_link = iter_delta_photos(token, drive_id, delta_link)

        uploaded = 0
        skipped = 0
        failed = 0
        photo_list_for_filter = []

        for photo in changed_photos:
            name = photo["name"]
            ext = os.path.splitext(name)[1].lower()
            item_id = photo["id"]
            item_drive_id = photo.get("_drive_id", drive_id)

            # 상대 경로 계산
            parent_path = photo.get("parentReference", {}).get("path", "")
            # /drives/{id}/root: 이후 경로만 추출
            if "root:" in parent_path:
                parent_path = parent_path.split("root:")[-1]
            relative_path = sanitize_blob_path(
                f"{parent_path}/{name}" if parent_path else name
            )

            photo_id = f"photo_{shop_id}_{relative_path.replace('/', '_').replace(' ', '_')}"

            try:
                # ── DB 중복 체크 ──
                # TODO: from services.cosmos_db import get_photo_by_id
                # existing = get_photo_by_id(shop_id, photo_id)
                existing = None  # 목업: 항상 신규로 처리 (실제 연동 시 위 코드 사용)
                if existing:
                    logger.info(f"[onedrive] ⏭️ 중복 스킵 (DB에 존재): {name}")
                    skipped += 1
                    continue

                # ── Blob 업로드 ──
                content_type = photo.get("file", {}).get("mimeType")
                if not content_type:
                    content_type, _ = mimetypes.guess_type(name)

                download_url = f"{GRAPH_BASE}/drives/{item_drive_id}/items/{item_id}/content"
                download_resp = stream_download_file(download_url, token)

                content_settings = ContentSettings(content_type=content_type)
                container_client.upload_blob(
                    name=relative_path,
                    data=download_resp.raw,
                    content_settings=content_settings,
                    overwrite=False  # 중복 체크를 DB에서 했으므로 덮어쓰기 불필요
                )

                blob_url = (
                    f"https://bybaekstorage.blob.core.windows.net"
                    f"/{container_name}/{quote(relative_path)}"
                )

                # ── Cosmos DB 저장 (Instagram 지원 포맷만) ──
                if ext in INSTAGRAM_SUPPORTED:
                    save_photo(shop_id, {
                        "photo_id":      photo_id,
                        "blob_url":      blob_url,
                        "onedrive_url":  download_url,
                        "name":          name,
                        "last_modified": photo.get("lastModifiedDateTime", "")
                    })
                    photo_list_for_filter.append({
                        "image_id": photo_id,
                        "blob_url": blob_url
                    })
                else:
                    logger.info(f"[onedrive] ⏭️ Instagram 미지원 포맷 스킵: {name}")

                logger.info(f"[onedrive] ✅ 업로드 성공: {name}")
                uploaded += 1

            except Exception as e:
                logger.error(f"[onedrive] ❌ 업로드 실패 ({name}): {e}")
                failed += 1

        # ── Delta Link 저장 (다음 동기화 때 변경분만 가져오기 위해) ──
        if next_delta_link:
            try:
                update_shop_onedrive_info(shop_id, {"delta_link": next_delta_link})
                logger.info(f"[onedrive] Delta Link 저장 완료")
            except Exception as e:
                logger.error(f"[onedrive] Delta Link 저장 실패 (동기화는 정상 완료): {e}")

        logger.info(
            f"[onedrive] 동기화 완료 → "
            f"업로드 {uploaded} / 스킵 {skipped} / 실패 {failed}"
        )

        # ── 백그라운드 필터링 (업로드 완료 즉시 응답, 필터링은 별도 실행) ──
        filter_started = False
        if photo_list_for_filter:
            background_tasks.add_task(
                _run_filter_background,
                shop_id=shop_id,
                photo_list=photo_list_for_filter
            )
            filter_started = True
            logger.info(f"[onedrive] 백그라운드 필터링 예약 → {len(photo_list_for_filter)}장")

        return SyncPhotosResponse(
            success=True,
            uploaded=uploaded,
            skipped=skipped,
            failed=failed,
            filter_started=filter_started,
            container=container_name,
        )

    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        logger.error(f"[onedrive] 동기화 전체 에러: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


# ──────────────────────────────────────────
# 백그라운드 필터링 (BackgroundTasks)
# ──────────────────────────────────────────

async def _run_filter_background(shop_id: str, photo_list: list):
    """
    업로드 완료 후 백그라운드에서 실행되는 필터링.
    FastAPI BackgroundTasks는 async를 직접 지원하므로 asyncio.run() 불필요.
    """
    logger.info(f"[onedrive] 백그라운드 필터링 시작 → {len(photo_list)}장")
    try:
        from agents.photo_filter import run_photo_filter
        result = await run_photo_filter(shop_id, photo_list)
        logger.info(
            f"[onedrive] 필터링 완료 → "
            f"1차 PASS {result.get('stage1_passed', 0)} / "
            f"2차 PASS {result.get('stage2_passed', 0)} / "
            f"전체 {result.get('total', 0)}"
        )
    except Exception as e:
        logger.error(f"[onedrive] 백그라운드 필터링 실패: {e}")
        traceback.print_exc()