from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


# 요청/응답 모델

class FilterTriggerRequest(BaseModel):
    shop_id: str
    force_refilter: bool = False    # True면 이미 필터링된 사진도 재처리


class FilterTriggerResponse(BaseModel):
    shop_id: str
    status: str                     # "started" | "error"
    total_photos: int
    message: str


class FilterStatusResponse(BaseModel):
    shop_id: str
    total: int
    stage1_passed: int
    stage2_passed: int
    pending: int                    # 아직 필터링 안 된 사진 수


# 엔드포인트 

@router.post("/filter", response_model=FilterTriggerResponse)
async def trigger_photo_filter(
    req: FilterTriggerRequest,
    background_tasks: BackgroundTasks
):
    """
    사진 필터링 트리거.

    OneDrive 동기화 완료 후 호출.
    CosmosDB Photo 컨테이너에서 미필터링 사진 목록 조회 → 백그라운드로 필터링 실행.

    백그라운드 실행이므로 즉시 "started" 응답 반환.
    진행 상황은 GET /api/photos/status 로 폴링.
    """
    try:
        photo_list = _get_unfiltered_photos(req.shop_id, req.force_refilter)

        if not photo_list:
            return FilterTriggerResponse(
                shop_id=req.shop_id,
                status="started",
                total_photos=0,
                message="필터링 대상 사진 없음"
            )

        # 백그라운드 실행 (응답 먼저 반환)
        background_tasks.add_task(
            _run_filter_background,
            shop_id=req.shop_id,
            photo_list=photo_list
        )

        print(f"[photos] 필터링 트리거 → shop_id={req.shop_id}, 대상={len(photo_list)}장")
        return FilterTriggerResponse(
            shop_id=req.shop_id,
            status="started",
            total_photos=len(photo_list),
            message=f"{len(photo_list)}장 필터링 시작"
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{shop_id}", response_model=FilterStatusResponse)
async def get_filter_status(shop_id: str):
    """
    필터링 진행 상황 조회. 프론트 폴링용.

    Photo 컨테이너에서 단계별 카운트 반환.
    """
    try:
        from services.cosmos_db import get_all_photos_by_shop

        photos = get_all_photos_by_shop(shop_id)

        total         = len(photos)
        stage1_passed = sum(1 for p in photos if p.get("stage1_pass") is True)
        stage2_passed = sum(1 for p in photos if p.get("is_usable") is True)
        pending       = sum(1 for p in photos if p.get("stage1_pass") is None)

        return FilterStatusResponse(
            shop_id=shop_id,
            total=total,
            stage1_passed=stage1_passed,
            stage2_passed=stage2_passed,
            pending=pending
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 헬퍼 
def _get_unfiltered_photos(shop_id: str, force_refilter: bool = False) -> list:
    """
    CosmosDB Photo 컨테이너에서 필터링 대상 사진 목록 조회.

    force_refilter=False: stage1_pass가 None인 사진만 (미처리)
    force_refilter=True : 모든 사진 재처리
    """
    from services.cosmos_db import get_all_photos_by_shop

    photos = get_all_photos_by_shop(shop_id)

    if not force_refilter:
        photos = [p for p in photos if p.get("stage1_pass") is None]

    return [
        {
            "image_id": p.get("id"),
            "blob_url": p.get("blob_url")
        }
        for p in photos
        if p.get("id") and p.get("blob_url")
    ]


async def _run_filter_background(shop_id: str, photo_list: list):
    """백그라운드 필터링 실행"""
    try:
        from agents.photo_filter import run_photo_filter
        result = await run_photo_filter(shop_id, photo_list)
        print(
            f"[photos] 필터링 완료 → shop_id={shop_id} | "
            f"전체={result['total']} | "
            f"1차={result['stage1_passed']} | "
            f"2차={result['stage2_passed']}"
        )
    except Exception as e:
        print(f"[photos] 백그라운드 필터링 실패 (shop_id={shop_id}): {e}")