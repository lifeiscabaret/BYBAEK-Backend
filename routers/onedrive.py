import mimetypes
import os
from typing import Dict, Generator, List, Optional

import msal
import requests
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
from azure.identity import DefaultAzureCredential 
from azure.storage.blob import BlobServiceClient, ContentSettings
from utils.logging import logger


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


def get_graph_token(easy_auth_token: str) -> str:

    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")

    authority = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_on_behalf_of(
        user_assertion=easy_auth_token,
        scopes=["https://graph.microsoft.com/User.Read"]
    )
    logger.info(result)

    access_token = result.get("access_token")
    logger.info(access_token)

    if not access_token:
        raise RuntimeError(f"Failed to acquire Graph token: {result}")

    return access_token


def graph_get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    headers = {
        "Authorization": f"Bearer {token}"
    }

    response = requests.get(url, headers=headers, params=params, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Graph GET failed: {response.status_code} {response.text}")

    return response.json()


def get_user_drive_id(token: str) -> str:
    data = graph_get(
        f"{GRAPH_BASE}/me/drive",
        token,
        params={"$select": "id"}
    )

    logger.info(data)
    return data["id"]


def iter_children(token: str, drive_id: str, item_id: str) -> Generator[Dict, None, None]:
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/children"
    params = {
        "$top": PAGE_SIZE,
        "$select": "id,name,folder,file,parentReference,@microsoft.graph.downloadUrl"
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
    stack: List[tuple[str, str]] = [(root_item_id, "")]

    while stack:
        current_item_id, current_path = stack.pop()

        for item in iter_children(token, drive_id, current_item_id):
            name = item["name"]
            rel_path = f"{current_path}/{name}" if current_path else name

            if "folder" in item:
                stack.append((item["id"], rel_path))
            elif is_photo(item):
                item["_relative_path"] = rel_path
                yield item


def stream_download_file(download_url: str) -> requests.Response:
    response = requests.get(download_url, stream=True, timeout=300)
    if response.status_code >= 400:
        raise RuntimeError(f"Download failed: {response.status_code} {response.text}")
    return response

@router.post(
    "/sync-photos",
    response_model=SyncPhotosResponse,
    status_code=status.HTTP_200_OK,
)
def sync_onedrive_photos(req: SyncPhotosRequest, request: Request) -> SyncPhotosResponse:
    try:

        aad_token = request.headers.get("x-ms-token-aad-access-token")
        logger.info(aad_token)

        token = get_graph_token(aad_token)
        logger.info(token)

        drive_id = get_user_drive_id(token)
        logger.info(drive_id)

        container_name = "photos"

        account_url = "https://stctrla.blob.core.windows.net"
        credential = DefaultAzureCredential()
        blob_service_client = BlobServiceClient(account_url, credential=credential)
        container_client = blob_service_client.get_container_client(container=container_name)

        uploaded = 0
        failed = 0
        skipped = 0

        for photo in walk_drive_for_photos(token, drive_id, req.root_folder_item_id):
            name = photo["name"]
            logger.info(name)

            download_url = photo.get("@microsoft.graph.downloadUrl")

            if not download_url:
                skipped += 1
                continue

            try:
                content_type = photo.get("file", {}).get("mimeType")
                if not content_type:
                    guessed, _ = mimetypes.guess_type(photo.get("name", ""))
                    content_type = guessed

                download_resp = stream_download_file(download_url)
                content_settings = ContentSettings(content_type=content_type)

                container_client.upload_blob(
                    name="",
                    data=download_resp.raw,
                    content_settings=content_settings,
                    overwrite=req.overwrite
                )

                uploaded += 1

            except Exception:
                failed += 1

        return SyncPhotosResponse(
            success=True,
            uploaded=uploaded,
            failed=failed,
            skipped=skipped,
            container=container_name,
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )