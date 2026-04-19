"""
역할: 사진 관련 라우터
- GET  /api/photos/all/{shop_id}
- GET  /api/photos/albums/{shop_id}
- GET  /api/photos/albums/{shop_id}/{album_id}
- POST /api/photos/albums
- POST /api/photos/filter
- GET  /api/photos/status/{shop_id}
- GET|HEAD /api/photos/proxy/{photo_id}/image.jpg  Instagram 업로드용 이미지 프록시
- POST /api/photos/filter/test/{shop_id}
- DELETE /api/photos/albums/{shop_id}/{album_id}
- DELETE /api/photos/{shop_id}/{photo_id}

[수정 이력]
- FILTER_CHUNK_SIZE: 10장씩 청크 분할
- proxy 엔드포인트: Instagram SAS URL 차단 문제 해결
- proxy HEAD 메서드 추가: Instagram URL 유효성 검사 통과
"""

import os
import httpx
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
import uuid
from typing import List
from services.cosmos_db import get_all_photos_by_shop
from services.cosmos_db import get_photos_by_album
from services.cosmos_db import get_album_list
from services.cosmos_db import save_album
from services.cosmos_db import delete_album_data
from services.cosmos_db import delete_photo_data
from datetime import datetime, timedelta, timezone
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

router = APIRouter()

FILTER_CHUNK_SIZE = 10
BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "https://bybaek-b-bzhhgzh8d2gthpb3.koreacentral-01.azurewebsites.net"
)


class FilterTriggerRequest(BaseModel):
    shop_id: str
    force_refilter: bool = False

class FilterTriggerResponse(BaseModel):
    shop_id: str
    status: str
    total: int
    message: str

class FilterStatusResponse(BaseModel):
    shop_id: str
    total: int
    passed: int
    failed: int
    pending: int
    status: str

class AlbumCreateRequest(BaseModel):
    shop_id: str
    album_id: str
    album_name: str
    photo_ids: List[str]
    description: str = ""


def _to_sas_url(blob_url: str, hours: int = 2) -> str:
    try:
        clean_url = blob_url.split("?")[0]
        path = clean_url.replace("https://bybaekstorage.blob.core.windows.net/", "")
        container, blob_name = path.split("/", 1)
        sas_token = generate_blob_sas(
            account_name="bybaekstorage",
            container_name=container,
            blob_name=blob_name,
            account_key=os.getenv("AZURE_STORAGE_KEY"),
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
        )
        return f"{clean_url}?{sas_token}"
    except Exception:
        return blob_url


def get_proxy_url(photo_id: str, shop_id: str) -> str:
    return f"{BACKEND_URL}/api/photos/proxy/{shop_id}/{photo_id}/image.jpg"


@router.get("/all/{shop_id}")
async def read_all_photos(shop_id: str):
    all_photos = get_all_photos_by_shop(shop_id)
    photos = [p for p in all_photos if p.get("is_usable") is True]
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"photos": photos}


@router.get("/albums/{shop_id}")
async def read_albums(shop_id: str):
    albums = get_album_list(shop_id)
    for a in albums:
        if a.get("thumbnail_url"):
            a["thumbnail_url"] = _to_sas_url(a["thumbnail_url"])
    return {"albums": albums}


@router.get("/albums/{shop_id}/{album_id}")
async def read_album_photos(shop_id: str, album_id: str):
    photos = get_photos_by_album(shop_id, album_id)
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"album_id": album_id, "photos": photos}


@router.post("/albums")
async def create_album(req: AlbumCreateRequest):
    photo_list = [{"photo_id": pid} for pid in req.photo_ids]
    actual_album_id = req.album_id
    if not actual_album_id or actual_album_id == "new":
        actual_album_id = str(uuid.uuid4())
    success = save_album(
        shop_id=req.shop_id,
        album_id=actual_album_id,
        photo_list=photo_list,
        album_name=req.album_name,
        description=req.description
    )
    if not success:
        raise HTTPException(status_code=500, detail="앨범 저장에 실패했습니다.")
    return {"status": "success", "album_id": actual_album_id}


@router.post("/filter", response_model=FilterTriggerResponse)
async def trigger_photo_filter(req: FilterTriggerRequest, background_tasks: BackgroundTasks):
    try:
        all_photos = get_all_photos_by_shop(req.shop_id)
        if req.force_refilter:
            photo_list = all_photos
        else:
            photo_list = [p for p in all_photos if p.get("stage1_pass") is None]

        if not photo_list:
            return FilterTriggerResponse(
                shop_id=req.shop_id, status="started", total=0,
                message="새로운 사진이 없습니다."
            )

        chunks = [photo_list[i: i + FILTER_CHUNK_SIZE] for i in range(0, len(photo_list), FILTER_CHUNK_SIZE)]
        for chunk in chunks:
            background_tasks.add_task(_run_filter_process, shop_id=req.shop_id, photo_list=chunk)

        print(f"[Photo Router] {len(photo_list)}장 → {len(chunks)}개 청크로 분할 등록")
        return FilterTriggerResponse(
            shop_id=req.shop_id,
            status="started",
            total=len(photo_list),
            message=f"{len(photo_list)}장을 {len(chunks)}개 청크로 나눠 백그라운드 처리 시작합니다."
        )
    except Exception as e:
        print(f"[Photo Router] Trigger Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{shop_id}", response_model=FilterStatusResponse)
async def get_filter_status(shop_id: str):
    try:
        all_photos = get_all_photos_by_shop(shop_id)
        if not all_photos:
            return FilterStatusResponse(shop_id=shop_id, total=0, passed=0, failed=0, pending=0, status="no_photos")

        passed  = sum(1 for p in all_photos if p.get("is_usable") is True)
        failed  = sum(1 for p in all_photos if p.get("is_usable") is False)
        pending = sum(1 for p in all_photos if p.get("stage1_pass") is None and p.get("is_usable") is None)
        current_status = "done" if pending == 0 else "in_progress"

        return FilterStatusResponse(
            shop_id=shop_id, total=len(all_photos),
            passed=passed, failed=failed, pending=pending, status=current_status
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"상태 조회 실패: {str(e)}")


@router.api_route("/proxy/{shop_id}/{photo_id}/image.jpg", methods=["GET", "HEAD"])
async def proxy_photo(shop_id: str, photo_id: str, request: Request):
    """
    Instagram 업로드용 이미지 프록시.
    GET: 이미지 스트리밍 반환
    HEAD: Instagram URL 유효성 검사 통과용 (이미지 다운로드 없이 헤더만 반환)
    """
    from services.cosmos_db import get_photo_by_id
    photo = get_photo_by_id(shop_id, photo_id)
    if not photo or not photo.get("blob_url"):
        raise HTTPException(status_code=404, detail="사진을 찾을 수 없습니다.")

    # Instagram이 HEAD 요청으로 URL 유효성 검사 → 헤더만 반환
    if request.method == "HEAD":
        return Response(
            headers={
                "content-type": "image/jpeg",
                "content-length": "1000000",
                "accept-ranges": "bytes"
            }
        )

    sas_url = _to_sas_url(photo["blob_url"], hours=1)
    async with httpx.AsyncClient() as client:
        resp = await client.get(sas_url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail="이미지 다운로드 실패")

    content_type = resp.headers.get("content-type", "image/jpeg")
    return StreamingResponse(iter([resp.content]), media_type=content_type)


@router.delete("/albums/{shop_id}/{album_id}")
async def delete_album(shop_id: str, album_id: str):
    success = delete_album_data(shop_id, album_id)
    if not success:
        raise HTTPException(status_code=500, detail="앨범 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "앨범이 삭제되었습니다."}


@router.delete("/{shop_id}/{photo_id}")
async def delete_photo(shop_id: str, photo_id: str):
    success = delete_photo_data(shop_id, photo_id)
    if not success:
        raise HTTPException(status_code=500, detail="사진 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "사진이 삭제되었습니다."}


@router.post("/filter/test/{shop_id}")
async def test_filter_sync(shop_id: str):
    try:
        from agents.photo_filter import run_photo_filter
        all_photos = get_all_photos_by_shop(shop_id)
        photo_list = [p for p in all_photos if p.get("stage1_pass") is None][:3]
        prepared = [
            {"image_id": p.get("id"), "blob_url": _to_sas_url(p.get("blob_url"))}
            for p in photo_list if p.get("blob_url")
        ]
        result = await run_photo_filter(shop_id=shop_id, photo_list=prepared)
        return {"status": "ok", "result": result}
    except Exception as e:
        import traceback
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}


async def _run_filter_process(shop_id: str, photo_list: list):
    print(f"[Photo Router] 청크 필터링 시작 ({len(photo_list)}장)")
    try:
        from agents.photo_filter import run_photo_filter
        prepared_list = [
            {"image_id": p.get("id") or p.get("photo_id"), "blob_url": _to_sas_url(p.get("blob_url"))}
            for p in photo_list if p.get("blob_url")
        ]
        result = await run_photo_filter(shop_id=shop_id, photo_list=prepared_list)
        print(f"[Photo Router] 청크 완료 → stage1={result['stage1_passed']}, stage2={result['stage2_passed']}")
    except Exception as e:
        print(f"[Photo Router] 청크 필터링 오류: {str(e)}")
        import traceback
        traceback.print_exc()