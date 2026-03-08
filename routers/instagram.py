from typing import Optional, Literal

import requests
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, HttpUrl

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


def create_image_container(
    ig_user_id: str,
    access_token: str,
    image_url: HttpUrl,
) -> str:
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "image_url": image_url,
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

@router.post("/upload", response_model=InstagramPhotoPublishResponse, status_code=status.HTTP_201_CREATED)
async def upload(req: InstagramPhotoPublishRequest):

    container_ids = []
    
    for url in req.image_urls:
        container_ids.append(create_image_container(req.user_id, req.access_token, url))

    creation_id = create_carousel_container(req.user_id, req.access_token, container_ids, req.caption)

    media_id = publish_container(req.user_id, creation_id, req.access_token)
    
    if not media_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "media_id not returned from Instagram publish"
            },
        )
    
    return {"media_id": media_id}