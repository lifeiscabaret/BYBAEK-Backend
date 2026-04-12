import os
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
import uuid
from typing import Optional, List
from services.cosmos_db import get_all_photos_by_shop

from services.cosmos_db import get_photos_by_album      # 앨범 상세 조회
from services.cosmos_db import get_album_list           # 앨범 목록 조회
from services.cosmos_db import save_album               # 새 앨범 만들기
from services.cosmos_db import delete_album_data        # 앨범 삭제
from services.cosmos_db import delete_photo_data        # 사진 삭제
from datetime import datetime, timedelta, timezone
from azure.storage.blob import generate_blob_sas, BlobSasPermissions


router = APIRouter()

# 요청/응답 모델

class FilterTriggerRequest(BaseModel):
    shop_id: str
    force_refilter: bool = False    # True면 이미 필터링된 사진도 재처리

class FilterTriggerResponse(BaseModel):
    shop_id: str
    status: str                     # "started" | "error"
    total: int
    message: str

class FilterStatusResponse(BaseModel):
    shop_id: str
    total: int
    passed: int                     # is_usable == True
    failed: int                     # is_usable == False
    pending: int                    # is_usable == None
    status: str                     # "done" | "in_progress" | "no_photos"

#엔드포인트
#Pydantic 모델 (새 앨범 만들 때 사용)
class AlbumCreateRequest(BaseModel):
    shop_id: str
    album_id: str
    album_name: str
    photo_ids: List[str]
    description: str = ""

# 1. 전체 사진 조회 (프론트엔드 Photos 화면용)
def _to_sas_url(blob_url: str, hours: int = 2) -> str:
    try:
        path = blob_url.replace("https://bybaekstorage.blob.core.windows.net/", "")
        container, blob_name = path.split("/", 1)
        sas_token = generate_blob_sas(
            account_name="bybaekstorage",
            container_name=container,
            blob_name=blob_name,
            account_key=os.getenv("AZURE_STORAGE_KEY"),
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
        )
        return f"{blob_url}?{sas_token}"
    except Exception:
        return blob_url

@router.get("/all/{shop_id}")
async def read_all_photos(shop_id: str):
    all_photos = get_all_photos_by_shop(shop_id)
    # ✅ is_usable=False(탈락)만 제외, None(대기)과 True(통과)는 표시
    photos = [p for p in all_photos if p.get("is_usable") is True]
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"photos": photos}

# 2. 앨범 목록 조회 (프론트엔드 Album 화면용)
@router.get("/albums/{shop_id}")
async def read_albums(shop_id: str):
    albums = get_album_list(shop_id)
    # ✅ 추가
    for a in albums:
        if a.get("thumbnail_url"):
            a["thumbnail_url"] = _to_sas_url(a["thumbnail_url"])
    return {"albums": albums}

# 3. 특정 앨범 내 사진 조회
@router.get("/albums/{shop_id}/{album_id}")
async def read_album_photos(shop_id: str, album_id: str):
    photos = get_photos_by_album(shop_id, album_id)
    # ✅ 추가
    for p in photos:
        if p.get("blob_url"):
            p["blob_url"] = _to_sas_url(p["blob_url"])
    return {"album_id": album_id, "photos": photos}

# 4. 새 앨범 생성 (사진들을 묶어서 앨범으로 저장)
@router.post("/albums")
async def create_album(req: AlbumCreateRequest):
    # photo_ids 리스트를 함수 형식에 맞게 변환 (dict 형태의 list)
    photo_list = [{"photo_id": pid} for pid in req.photo_ids]
    
    # album_id가 "new"이거나 없으면 새로 생성
    actual_album_id = req.album_id
    if not actual_album_id or actual_album_id == "new":
        actual_album_id = str(uuid.uuid4()) # 새 UUID 생성

    success = save_album(
        shop_id=req.shop_id, 
        album_id=actual_album_id, # None으로 주면 함수에서 자동 생성함
        photo_list=photo_list, 
        album_name=req.album_name,
        description=req.description
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="앨범 저장에 실패했습니다.")
    
    return {"status": "success", "album_id": actual_album_id}

@router.post("/filter", response_model=FilterTriggerResponse)
async def trigger_photo_filter(
    req: FilterTriggerRequest,
    background_tasks: BackgroundTasks
):
    try:
        all_photos = get_all_photos_by_shop(req.shop_id)
        
        if req.force_refilter:
            photo_list = all_photos
        else:
            photo_list = [p for p in all_photos if p.get("stage1_pass") is None]

        if not photo_list:
            return FilterTriggerResponse(
                shop_id=req.shop_id, status="started", total=0, message="새로운 사진이 없습니다."
            )

        # 다시 백그라운드 방식으로 복구
        background_tasks.add_task(
            _run_filter_process,
            shop_id=req.shop_id,
            photo_list=photo_list
        )

        return FilterTriggerResponse(
            shop_id=req.shop_id,
            status="started",
            total=len(photo_list),
            message=f"{len(photo_list)}장의 사진에 대해 필터링을 백그라운드에서 시작합니다."
        )

    except Exception as e:
        print(f"[Photo Router] Trigger Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{shop_id}", response_model=FilterStatusResponse)
async def get_filter_status(shop_id: str):
    """
    [STEP 2] 필터링 진행 상황 조회
    프론트엔드에서 폴링(Polling)하여 'done'이 될 때까지 확인합니다.
    """
    try:
        all_photos = get_all_photos_by_shop(shop_id)
        
        if not all_photos:
            return FilterStatusResponse(
                shop_id=shop_id, total=0, passed=0, failed=0, pending=0, status="no_photos"
            )

        # 상태 집계 (is_usable 기준)
        passed  = sum(1 for p in all_photos if p.get("is_usable") is True)
        failed  = sum(1 for p in all_photos if p.get("is_usable") is False)
        # 1차 필터링 결과조차 없는 사진들을 대기 중으로 판단
        pending = sum(1 for p in all_photos if p.get("stage1_pass") is None)

        # stage1_pass=None 이면서 is_usable=None → 아직 처리 안 된 사진
        # stage1_pass=False or True 이고 is_usable=None → 1차 실패 or 2차 대기
        # 완료 기준: is_usable이 결정된 사진 + stage1_pass=False 사진을 제외한 미처리가 없을 때
        pending = sum(1 for p in all_photos
                      if p.get("stage1_pass") is None and p.get("is_usable") is None)
        current_status = "done" if pending == 0 else "in_progress"

        return FilterStatusResponse(
            shop_id=shop_id,
            total=len(all_photos),
            passed=passed,
            failed=failed,
            pending=pending,
            status=current_status
        )

    except Exception as e:
        print(f"[Photo Router] Status Error: {e}")
        raise HTTPException(status_code=500, detail=f"상태 조회 실패: {str(e)}")


# --- 내부 헬퍼 함수 ---

async def _run_filter_process(shop_id: str, photo_list: list):
    print(f"DEBUG: 1. 프로세스 진입 (shop_id: {shop_id})")
    try:
        from agents.photo_filter import run_photo_filter
        print("DEBUG: 2. 에이전트 임포트 성공")

        prepared_list = [
            {"image_id": p.get("id") or p.get("photo_id"), "blob_url": p.get("blob_url")}
            for p in photo_list if p.get("blob_url")
        ]
        print(f"DEBUG: 3. 사진 준비 완료 ({len(prepared_list)}장)")

        result = await run_photo_filter(shop_id=shop_id, photo_list=prepared_list)
        print(f"DEBUG: 4. 완료 → stage1={result['stage1_passed']}, stage2={result['stage2_passed']}")

    except Exception as e:
        print(f"❌ DEBUG ERROR: {str(e)}")
        import traceback
        traceback.print_exc()


# 앨범 삭제 API
@router.delete("/albums/{shop_id}/{album_id}")
async def delete_album(shop_id: str, album_id: str):
    success = delete_album_data(shop_id, album_id)
    if not success:
        raise HTTPException(status_code=500, detail="앨범 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "앨범이 삭제되었습니다."}

# 사진 삭제 API
@router.delete("/{shop_id}/{photo_id}")
async def delete_photo(shop_id: str, photo_id: str):
    success = delete_photo_data(shop_id, photo_id)
    if not success:
        raise HTTPException(status_code=500, detail="사진 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "사진이 삭제되었습니다."}