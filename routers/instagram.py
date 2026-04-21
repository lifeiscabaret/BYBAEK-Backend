import io
import os
import time
import uuid
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException, status
from PIL import Image
from pydantic import BaseModel, HttpUrl
from azure.storage.blob import BlobServiceClient, ContentSettings

from services.blob_storage import generate_sas_url
from utils.logging import logger

router = APIRouter()

GRAPH_BASE = "https://graph.instagram.com/v25.0"

publish_check_retries: int = 10
publish_check_interval_sec: float = 2.0

# Instagram Graph API 허용 비율 범위
INSTA_MIN_RATIO = 0.8        # 4:5 세로 (최대 세로)
INSTA_MAX_RATIO = 1.9099     # 1.91:1 가로 (최대 가로)


class InstagramPhotoPublishRequest(BaseModel):
    user_id: str
    access_token: str
    image_urls: list[HttpUrl]
    caption: str

class InstagramPhotoPublishResponse(BaseModel):
    media_id: str


# ── 비율 정규화 ───────────────────────────────────────────────────────────────

def _normalize_aspect_ratio(blob_url: str) -> str:
    """
    이미지 비율이 Instagram 허용 범위(4:5 ~ 1.91:1)를 벗어나면
    중앙 크롭 후 Blob Storage에 임시 저장하고 새 URL 반환.
    범위 안이면 원본 URL 그대로 반환.

    크롭 기준:
    - 너무 세로로 긴 사진 (ratio < 0.8) → 4:5 비율로 중앙 크롭
    - 너무 가로로 넓은 사진 (ratio > 1.91) → 1.91:1 비율로 중앙 크롭
    """
    from agents.photo_filter import _generate_sas_url

    try:
        sas_url = _generate_sas_url(blob_url)
        resp = requests.get(sas_url, timeout=30)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        logger.warning(f"[instagram] 이미지 다운로드 실패 ({blob_url}): {e} → 원본 사용")
        return blob_url

    w, h = img.size
    ratio = w / h
    logger.info(f"[instagram] 이미지 비율 확인 → {w}x{h} ({ratio:.3f})")

    if INSTA_MIN_RATIO <= ratio <= INSTA_MAX_RATIO:
        logger.info(f"[instagram] 비율 정상 → 원본 사용")
        return blob_url

    # 크롭 목표 비율 결정
    if ratio < INSTA_MIN_RATIO:
        # 너무 세로 → 4:5로 크롭
        target_ratio = INSTA_MIN_RATIO
        new_h = int(w / target_ratio)
        new_w = w
        logger.info(f"[instagram] 세로 초과 ({ratio:.3f}) → 4:5 크롭: {new_w}x{new_h}")
    else:
        # 너무 가로 → 1.91:1로 크롭
        target_ratio = INSTA_MAX_RATIO
        new_w = int(h * target_ratio)
        new_h = h
        logger.info(f"[instagram] 가로 초과 ({ratio:.3f}) → 1.91:1 크롭: {new_w}x{new_h}")

    # 중앙 크롭
    left   = (w - new_w) // 2
    top    = (h - new_h) // 2
    right  = left + new_w
    bottom = top + new_h
    img = img.crop((left, top, right, bottom))

    # 메모리에서 JPEG 변환
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    buf.seek(0)

    # Blob Storage에 임시 저장
    try:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name    = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")
        temp_blob_name    = f"temp_cropped/{uuid.uuid4().hex}.jpg"

        blob_service = BlobServiceClient.from_connection_string(connection_string)
        blob_client  = blob_service.get_blob_client(container=container_name, blob=temp_blob_name)
        blob_client.upload_blob(
            buf,
            overwrite=True,
            content_settings=ContentSettings(content_type="image/jpeg")
        )

        new_url = f"https://bybaekstorage.blob.core.windows.net/{container_name}/{temp_blob_name}"
        logger.info(f"[instagram] 크롭 이미지 업로드 완료 → {new_url}")
        return new_url

    except Exception as e:
        logger.warning(f"[instagram] 크롭 이미지 업로드 실패 ({e}) → 원본 사용")
        return blob_url


def _cleanup_temp_blobs(urls: list[str]):
    """업로드 완료 후 temp_cropped/ 임시 파일 삭제."""
    try:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name    = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")
        blob_service      = BlobServiceClient.from_connection_string(connection_string)

        for url in urls:
            if "temp_cropped/" in url:
                blob_name = url.split(f"{container_name}/")[-1]
                blob_service.get_blob_client(container=container_name, blob=blob_name).delete_blob()
                logger.info(f"[instagram] 임시 파일 삭제 → {blob_name}")
    except Exception as e:
        logger.warning(f"[instagram] 임시 파일 삭제 실패 (무시): {e}")


# ── Graph API 헬퍼 ────────────────────────────────────────────────────────────

def graph_post(endpoint: str, headers: dict, data: dict) -> dict:
    url  = f"{GRAPH_BASE}{endpoint}"
    resp = requests.post(url, headers=headers, data=data, timeout=60)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Instagram Graph API request failed",
                "endpoint": endpoint,
                "status_code": resp.status_code,
                "response": body,
            },
        )
    return body


def graph_get(endpoint: str, headers: dict, params: dict) -> dict:
    url  = f"{GRAPH_BASE}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    try:
        body = resp.json()
    except Exception:
        body = {"raw_text": resp.text}

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Instagram Graph API request failed",
                "endpoint": endpoint,
                "status_code": resp.status_code,
                "response": body,
            },
        )
    return body


def wait_until_ready(ig_user_id: str, creation_id: str, access_token: str) -> None:
    """컨테이너 처리 완료될 때까지 폴링."""
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(publish_check_retries):
        body        = graph_get(f"/{creation_id}", headers=headers,
                                params={"fields": "status_code", "access_token": access_token})
        status_code = body.get("status_code")
        logger.info(f"[instagram] 컨테이너 상태 ({attempt+1}/{publish_check_retries}): {status_code}")
        if status_code == "FINISHED":
            return
        if status_code == "ERROR":
            raise HTTPException(status_code=500,
                detail={"message": "Instagram 미디어 처리 실패", "response": body})
        time.sleep(publish_check_interval_sec)
    raise HTTPException(status_code=500,
        detail={"message": f"Instagram 미디어 처리 타임아웃 ({publish_check_retries * publish_check_interval_sec}초 초과)"})


def create_image_container(ig_user_id: str, access_token: str,
                           image_url: str, is_carousel_item: bool = False) -> str:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    data    = {"image_url": str(image_url)}
    if is_carousel_item:
        data["is_carousel_item"] = "true"

    result     = graph_post(f"/{ig_user_id}/media", headers=headers, data=data)
    creation_id = result.get("id")
    if not creation_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "creation_id not returned from Instagram", "response": result})
    return creation_id


def create_carousel_container(ig_user_id: str, access_token: str,
                              container_ids: list[str], caption: str) -> str:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    data    = {"caption": caption, "media_type": "CAROUSEL", "children": ",".join(container_ids)}

    result      = graph_post(f"/{ig_user_id}/media", headers=headers, data=data)
    creation_id = result.get("id")
    if not creation_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "creation_id not returned from Instagram", "response": result})
    return creation_id


def publish_container(ig_user_id: str, creation_id: str, access_token: str) -> str:
    headers  = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    data     = {"creation_id": creation_id}
    result   = graph_post(f"/{ig_user_id}/media_publish", headers=headers, data=data)
    media_id = result.get("id")
    if not media_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "media_id not returned from Instagram publish", "response": result})
    return media_id


# ── 메인 업로드 함수 ──────────────────────────────────────────────────────────

def publish_photos(ig_user_id: str, access_token: str,
                   image_urls: list, caption: str) -> str:
    """
    사진 수에 따라 단일/캐러셀 자동 분기.
    업로드 전 비율 자동 정규화 (4:5 ~ 1.91:1 범위 벗어나면 중앙 크롭).
    """
    # 비율 정규화 (범위 벗어난 사진만 크롭 후 임시 URL 생성)
    normalized_urls = [_normalize_aspect_ratio(url) for url in image_urls]
    temp_urls       = [u for u in normalized_urls if "temp_cropped/" in u]

    try:
        if len(normalized_urls) == 1:
            creation_id = create_image_container(ig_user_id, access_token,
                                                 normalized_urls[0], is_carousel_item=False)
            wait_until_ready(ig_user_id, creation_id, access_token)
            media_id = publish_container(ig_user_id, creation_id, access_token)
            logger.info(f"[instagram] 단일 이미지 업로드 성공 → media_id={media_id}")
        else:
            container_ids = [
                create_image_container(ig_user_id, access_token, url, is_carousel_item=True)
                for url in normalized_urls
            ]
            creation_id = create_carousel_container(ig_user_id, access_token, container_ids, caption)
            wait_until_ready(ig_user_id, creation_id, access_token)
            media_id = publish_container(ig_user_id, creation_id, access_token)
            logger.info(f"[instagram] 캐러셀 업로드 성공 ({len(normalized_urls)}장) → media_id={media_id}")

    finally:
        # 임시 크롭 파일 정리
        if temp_urls:
            _cleanup_temp_blobs(temp_urls)

    return media_id


@router.post("/upload", response_model=InstagramPhotoPublishResponse,
             status_code=status.HTTP_201_CREATED)
async def upload(req: InstagramPhotoPublishRequest):
    sas_urls = [generate_sas_url(str(url)) for url in req.image_urls]
    media_id = publish_photos(
        ig_user_id=req.user_id,
        access_token=req.access_token,
        image_urls=sas_urls,
        caption=req.caption,
    )
    if not media_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "media_id not returned from Instagram publish"})
    return {"media_id": media_id}