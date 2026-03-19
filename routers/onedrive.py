import mimetypes
import os
from typing import Dict, Generator, List, Optional

import requests
import traceback
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient, ContentSettings
from utils.logging import logger
from services.cosmos_db import save_photo


router = APIRouter()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
PAGE_SIZE = 200


class SyncPhotosResponse(BaseModel):
    success: bool
    uploaded: int
    failed: int
    skipped: int
    container: str


class SyncPhotosRequest(BaseModel):
    target_user_principal_name: Optional[str] = None
    root_folder_item_id: str = "root"
    overwrite: bool = True


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


def iter_children(token: str, drive_id: str, item_id: str) -> Generator[Dict, None, None]:
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children"
    params = {
        "$top": PAGE_SIZE,
        "$select": "id,name,folder,file,parentReference,lastModifiedDateTime"
    }

    while url:
        data = graph_get(url, token, params=params)
        for item in data.get("value", []):
            yield item
        url = data.get("@odata.nextLink")
        params = None


def is_photo(item: Dict) -> bool:
    if "file" not in item:
        return False
    name = item.get("name", "")
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXTENSIONS:
        return True
    mime_type = item.get("file", {}).get("mimeType", "")
    return mime_type.startswith("image/")


def sanitize_blob_path(path: str) -> str:
    return path.strip("/").replace("\\", "/")


def walk_drive_for_photos(token: str, drive_id: str, root_item_id: str = "root") -> Generator[Dict, None, None]:
    stack: List[tuple] = [(root_item_id, "")]

    while stack:
        current_item_id, current_path = stack.pop()
        for item in iter_children(token, drive_id, current_item_id):
            name = item["name"]
            rel_path = f"{current_path}/{name}" if current_path else name
            if "folder" in item:
                stack.append((item["id"], rel_path))
            elif is_photo(item):
                item["_relative_path"] = rel_path
                item["_drive_id"] = drive_id  # drive_id 함께 전달
                yield item


def stream_download_file(download_url: str, token: str = None) -> requests.Response:
    """
    파일 다운로드
    - token이 있으면 Graph API content 엔드포인트 (인증 필요)
    - token이 없으면 직접 URL (downloadUrl 방식)
    """
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(download_url, headers=headers, stream=True, timeout=300)
    if response.status_code >= 400:
        raise RuntimeError(f"Download failed: {response.status_code} {response.text[:100]}")
    return response


@router.post(
    "/sync-photos",
    response_model=SyncPhotosResponse,
    status_code=status.HTTP_200_OK,
)
def sync_onedrive_photos(req: SyncPhotosRequest, request: Request) -> SyncPhotosResponse:
    try:
        # Easy Auth 토큰 = 이미 Graph 토큰, OBO 불필요
        token = request.headers.get("x-ms-token-aad-access-token")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MS 로그인 필요. x-ms-token-aad-access-token 헤더 없음."
            )
        logger.info(f"[onedrive] Graph 토큰 직접 사용 (OBO 없음)")

        drive_id = get_user_drive_id(token)
        shop_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "unknown")
        logger.info(f"[onedrive] shop_id: {shop_id}")

        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client(container=container_name)

        uploaded = 0
        failed = 0
        skipped = 0

        for photo in walk_drive_for_photos(token, drive_id, req.root_folder_item_id):
            name = photo["name"]
            relative_path = sanitize_blob_path(photo.get("_relative_path", name))
            item_id = photo["id"]
            item_drive_id = photo.get("_drive_id", drive_id)

            try:
                content_type = photo.get("file", {}).get("mimeType")
                if not content_type:
                    guessed, _ = mimetypes.guess_type(name)
                    content_type = guessed

                # ✅ downloadUrl 대신 Graph API content 엔드포인트 사용 (항상 작동)
                download_url = f"{GRAPH_BASE}/drives/{item_drive_id}/items/{item_id}/content"
                download_resp = stream_download_file(download_url, token=token)

                content_settings = ContentSettings(content_type=content_type)
                container_client.upload_blob(
                    name=relative_path,
                    data=download_resp.raw,
                    content_settings=content_settings,
                    overwrite=req.overwrite
                )

                blob_url = f"https://stctrla.blob.core.windows.net/{container_name}/{relative_path}"
                photo_id = f"photo_{shop_id}_{relative_path.replace('/', '_').replace(' ', '_')}"

                save_photo(shop_id, {
                    "photo_id":      photo_id,
                    "blob_url":      blob_url,
                    "onedrive_url":  download_url,
                    "name":          name,
                    "last_modified": photo.get("lastModifiedDateTime", "")
                })

                logger.info(f"[onedrive] ✅ 업로드 성공: {name}")
                uploaded += 1

            except Exception as e:
                logger.error(f"[onedrive] ❌ 업로드 실패 ({name}): {e}")
                failed += 1

        logger.info(f"[onedrive] 동기화 완료 | uploaded={uploaded} failed={failed} skipped={skipped}")

        return SyncPhotosResponse(
            success=True,
            uploaded=uploaded,
            failed=failed,
            skipped=skipped,
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