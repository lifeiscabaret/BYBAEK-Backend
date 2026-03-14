"""
역할: 사진 필터링 요청 및 상태 조회 라우터
흐름: OneDrive 동기화 완료 후 호출 -> 백그라운드 필터링(1,2차) 실행 -> 프론트엔드 폴링으로 결과 확인
"""

import os
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, List
from services.cosmos_db import get_all_photos_by_shop

router = APIRouter()

# --- 요청/응답 모델 ---

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

# --- 엔드포인트 ---

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

        # ✅ 다시 백그라운드 방식으로 복구!
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

        # 1차 통과자 중 2차(is_usable)가 결정되지 않은게 없는지 확인하는 로직 추가 예정
        # pending == 0 을 완료 기준으로 잡음.
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
        from agents.photo_filter import run_stage2_filter
        print("DEBUG: 2. 에이전트 임포트 성공")

        prepared_list = [
            {"image_id": p.get("id") or p.get("photo_id"), "blob_url": p.get("blob_url")}
            for p in photo_list if p.get("blob_url")
        ]
        print(f"DEBUG: 3. 사진 준비 완료 ({len(prepared_list)}장)")

        result = await run_stage2_filter(shop_id=shop_id, stage1_pass_list=prepared_list)
        print(f"DEBUG: 4. 결과: {result}")

    except Exception as e:
        print(f"❌ DEBUG ERROR: {str(e)}")
        import traceback
        traceback.print_exc()