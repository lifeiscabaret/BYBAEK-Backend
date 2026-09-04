"""
역할: 사진 관련 라우터
- GET  /api/photos/all/{shop_id}
- GET  /api/photos/before-after-pool/{shop_id}
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
from fastapi import APIRouter, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
import uuid
from typing import List
from auth.token_verify import get_current_shop, require_shop_owner
from services.cosmos_db import get_all_photos_by_shop
from services.cosmos_db import get_photos_by_album
from services.cosmos_db import get_album_list
from services.cosmos_db import save_album
from services.cosmos_db import delete_album_data
from services.cosmos_db import delete_photo_data
from datetime import datetime, timedelta, timezone
from azure.storage.blob import generate_blob_sas, BlobSasPermissions, BlobServiceClient
from utils.logging import logger

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
    """bare blob URL → 읽기 SAS URL.

    계정 키는 AZURE_STORAGE_CONNECTION_STRING 에서 도출한다.
    (예전엔 미설정 env AZURE_STORAGE_KEY 를 써서 항상 except→bare 폴백되고 있었음)
    컨테이너 비공개 전환 후 이 함수가 실패하면 이미지가 깨지므로, 실패는 경고 로그로 드러낸다.
    """
    try:
        clean_url = blob_url.split("?")[0]
        blob_service = BlobServiceClient.from_connection_string(
            os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        )
        account_name = blob_service.account_name
        account_key = blob_service.credential.account_key
        path = clean_url.split(".blob.core.windows.net/", 1)[1]
        container, blob_name = path.split("/", 1)
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
        )
        return f"{clean_url}?{sas_token}"
    except Exception as e:
        logger.warning(f"[photos] SAS 생성 실패 → bare URL 반환 ({blob_url}): {e}")
        return blob_url


def get_proxy_url(photo_id: str, shop_id: str) -> str:
    return f"{BACKEND_URL}/api/photos/proxy/{shop_id}/{photo_id}/image.jpg"


@router.get("/all/{shop_id}")
async def read_all_photos(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    all_photos = get_all_photos_by_shop(shop_id)
    photos = [p for p in all_photos if p.get("is_usable") is True]
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"photos": photos}


@router.get("/before-after-pool/{shop_id}")
async def read_before_after_pool(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    """비포/애프터 선택 전용 추가 풀.

    1차 런칭 스코프에서 필터 탈락(is_usable=false)했지만 '이 샵 관련은 있으나 바버샵
    스타일만 아닌' 사진(photo_category='other_service', 예: 롱헤어·펌·염색)만 반환한다.
    비포/애프터는 사장님이 직접 두 장을 골라 지정하는 수동 기능이라 AI 재심사가 불필요하며,
    '비포'는 본질적으로 다듬어지기 전 상태라 일반 필터로는 통과하지 못하는 병목을 해소한다.

    - /photos/all(is_usable=true, 애프터 후보)과는 별개 풀. 프론트에서 두 응답을 합쳐 사용.
    - irrelevant(강아지·스크린샷 등)나 품질 탈락분은 포함하지 않음
      (탈락 사진에는 photo_category가 저장되지 않아 other_service만 정확히 매칭됨).
    """
    require_shop_owner(current_shop, shop_id)
    all_photos = get_all_photos_by_shop(shop_id)
    pool = [
        p for p in all_photos
        if p.get("is_usable") is False and p.get("photo_category") == "other_service"
    ]
    for p in pool:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"photos": pool}


@router.get("/albums/{shop_id}")
async def read_albums(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    albums = get_album_list(shop_id)
    for a in albums:
        if a.get("thumbnail_url"):
            a["thumbnail_url"] = _to_sas_url(a["thumbnail_url"])
    return {"albums": albums}


@router.get("/albums/{shop_id}/{album_id}")
async def read_album_photos(shop_id: str, album_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    photos = get_photos_by_album(shop_id, album_id)
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"album_id": album_id, "photos": photos}


@router.post("/albums")
async def create_album(req: AlbumCreateRequest, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, req.shop_id)
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
async def trigger_photo_filter(req: FilterTriggerRequest, background_tasks: BackgroundTasks, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, req.shop_id)
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
async def get_filter_status(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
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
    return Response(
        content=resp.content,
        media_type=content_type,
        headers={
            "content-length": str(len(resp.content)),
            "accept-ranges": "bytes",
            "cache-control": "public, max-age=3600"
        }
    )


@router.delete("/albums/{shop_id}/{album_id}")
async def delete_album(shop_id: str, album_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    success = delete_album_data(shop_id, album_id)
    if not success:
        raise HTTPException(status_code=500, detail="앨범 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "앨범이 삭제되었습니다."}


@router.delete("/{shop_id}/{photo_id}")
async def delete_photo(shop_id: str, photo_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    success = delete_photo_data(shop_id, photo_id)
    if not success:
        raise HTTPException(status_code=500, detail="사진 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "사진이 삭제되었습니다."}


@router.post("/filter/test/{shop_id}")
async def test_filter_sync(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
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
        # 스택트레이스/내부 예외 메시지는 서버 로그에만 남긴다.
        # 응답 본문에 실으면 파일 경로·코드 구조·라이브러리 버전이 그대로 노출된다.
        logger.error(f"[photos] 필터 테스트 실패 (shop_id={shop_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="사진 처리 중 오류가 발생했습니다.")


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