import time
from typing import Optional, Literal

import requests
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, HttpUrl
from utils.logging import logger
from services.blob_storage import generate_sas_url

router = APIRouter()

GRAPH_BASE = "https://graph.instagram.com/v25.0"

publish_check_retries: int = 10
publish_check_interval_sec: float = 2.0

class InstagramPhotoPublishRequest(BaseModel):
    user_id: str
    access_token: str
    image_urls: list[HttpUrl]
    caption: str

class InstagramPhotoPublishResponse(BaseModel):
    media_id: str


def graph_post(endpoint: str, headers: dict, data: dict) -> dict:
    url = f"{GRAPH_BASE}{endpoint}"
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
    url = f"{GRAPH_BASE}{endpoint}"
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


def wait_until_ready(
    ig_user_id: str,
    creation_id: str,
    access_token: str,
) -> None:
    """컨테이너 처리 완료될 때까지 폴링 (최대 retries * interval초 대기)"""
    headers = {"Authorization": f"Bearer {access_token}"}
    for attempt in range(publish_check_retries):
        body = graph_get(
            f"/{creation_id}",
            headers=headers,
            params={"fields": "status_code", "access_token": access_token},
        )
        status_code = body.get("status_code")
        logger.info(f"[instagram] 컨테이너 상태 확인 ({attempt+1}/{publish_check_retries}): {status_code}")
        if status_code == "FINISHED":
            return
        if status_code == "ERROR":
            raise HTTPException(
                status_code=500,
                detail={"message": "Instagram 미디어 처리 실패", "response": body}
            )
        time.sleep(publish_check_interval_sec)
    raise HTTPException(
        status_code=500,
        detail={"message": f"Instagram 미디어 처리 타임아웃 ({publish_check_retries * publish_check_interval_sec}초 초과)"}
    )


def create_image_container(
    ig_user_id: str,
    access_token: str,
    image_url: HttpUrl,
    is_carousel_item: bool = False,
) -> str:
        
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "image_url": f'{image_url}',
    }

    if is_carousel_item:
        data["is_carousel_item"] = "true"

    result = graph_post(f"/{ig_user_id}/media", headers=headers, data=data)

    creation_id = result.get("id")
    if not creation_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "creation_id not returned from Instagram",
                "response": result,
            },
        )
    return creation_id


def create_carousel_container(
    ig_user_id: str,
    access_token: str,
    container_ids: list[str],
    caption: str,
) -> str:
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "caption": caption,
        "media_type": "CAROUSEL",
        "children": ",".join(container_ids)
    }

    result = graph_post(f"/{ig_user_id}/media", headers=headers, data=data)

    creation_id = result.get("id")
    if not creation_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "creation_id not returned from Instagram",
                "response": result,
            },
        )
    return creation_id


def publish_container(
    ig_user_id: str,
    creation_id: str,
    access_token: str,
) -> str:
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "creation_id": creation_id,
    }

    result = graph_post(f"/{ig_user_id}/media_publish", headers=headers, data=data)

    media_id = result.get("id")
    if not media_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "media_id not returned from Instagram publish",
                "response": result,
            },
        )
    return media_id


def publish_photos(
    ig_user_id: str,
    access_token: str,
    image_urls: list,
    caption: str,
) -> str:
    """
    사진 수에 따라 단일/캐러셀 자동 분기
    """
    if len(image_urls) == 1:
        # 단일 이미지
        creation_id = create_image_container(ig_user_id, access_token, image_urls[0], is_carousel_item=False)
        wait_until_ready(ig_user_id, creation_id, access_token)  # 처리 완료 대기
        media_id = publish_container(ig_user_id, creation_id, access_token)
        logger.info(f"[instagram] 단일 이미지 업로드 성공 → media_id={media_id}")
    else:
        # CAROUSEL (2장 이상)
        container_ids = [
            create_image_container(ig_user_id, access_token, url, is_carousel_item=True)
            for url in image_urls
        ]
        creation_id = create_carousel_container(ig_user_id, access_token, container_ids, caption)
        wait_until_ready(ig_user_id, creation_id, access_token)  # 처리 완료 대기
        media_id = publish_container(ig_user_id, creation_id, access_token)
        logger.info(f"[instagram] 캐러셀 업로드 성공 ({len(image_urls)}장) → media_id={media_id}")

    return media_id


@router.post("/upload", response_model=InstagramPhotoPublishResponse, status_code=status.HTTP_201_CREATED)
async def upload(req: InstagramPhotoPublishRequest):
    sas_urls = [generate_sas_url(str(url)) for url in req.image_urls]
    
    media_id = publish_photos(
        ig_user_id=req.user_id,
        access_token=req.access_token,
        image_urls=sas_urls,
        caption=req.caption,
    )
    
    if not media_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "media_id not returned from Instagram publish"}
        )
    
    return {"media_id": media_id}