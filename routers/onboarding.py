"""
온보딩 라우터
- POST /api/onboarding: 스무고개 설문 저장
- GET /api/onboarding/{shop_id}: 온보딩 데이터 조회
- POST /api/onboarding/reference: 레퍼런스 사진 저장
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from services.cosmos_db import save_onboarding as save_onboarding_db
from services.cosmos_db import get_onboarding as get_onboarding_db

router = APIRouter()

class PhotoRange(BaseModel):
    min: int = 1
    max: int = 5

class Schedule(BaseModel):
    upload_time: str
    frequency: str
    photo_range: PhotoRange
    timezone: str = "Asia/Seoul"

class OnboardingRequest(BaseModel):
    shop_id: str
    brand_tone: str
    forbidden_words: List[str]
    cta: str
    schedule: Schedule
    preferred_styles: Optional[List[str]] = []
    upload_mood: Optional[str] = ""

class ReferencePhotoRequest(BaseModel):
    shop_id: str
    photo_ids: List[str]  # 사장님이 선택한 레퍼런스 사진 ID 리스트 (3장)
    label: str = "good"    # "good" 고정 (나쁜 예시는 없음)

# @router.post("")
# async def save_onboarding(req: OnboardingRequest):
#     return {"shop_id": req.shop_id, "status": "success"}

# @router.get("/{shop_id}")
# async def get_onboarding(shop_id: str):
#     return {"shop_id": shop_id, "status": "mock"}

@router.post("/reference")
async def save_reference_photos(req: ReferencePhotoRequest):
    """
    온보딩 단계에서 사장님이 선택한 레퍼런스 사진 3장을 저장합니다.
    
    이 레퍼런스 사진들은 photo_filter.py의 2차 필터링에서
    "이 샵이 선호하는 스타일"을 GPT Vision이 학습하는 데 사용됩니다.
    
    Args:
        req.shop_id: 샵 ID
        req.photo_ids: 레퍼런스로 지정할 사진 ID 리스트 (3장)
        req.label: "good" 고정
    
    Returns:
        {"shop_id": str, "saved_count": int, "status": "success"}
    """
    try:
        from services.cosmos_db import save_album
        
        # 레퍼런스 앨범 정보
        album_id = f"reference_{req.shop_id}"
        album_name = "Reference Photos"
        description = f"photo_filter 2차 필터링 학습용 레퍼런스 ({req.label})"
        
        # photo_list 구성 (save_album이 기대하는 형식)
        photo_list = [{"photo_id": pid} for pid in req.photo_ids]
        
        # save_album 호출
        success = save_album(
            shop_id=req.shop_id,
            album_id=album_id,
            photo_list=photo_list,
            album_name=album_name,
            description=description
        )
        
        if not success:
            raise HTTPException(
                status_code=500,
                detail="레퍼런스 앨범 저장 실패"
            )
        
        return {
            "shop_id": req.shop_id,
            "saved_count": len(req.photo_ids),
            "album_id": album_id,
            "status": "success"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[onboarding] 레퍼런스 저장 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"레퍼런스 사진 저장 중 오류 발생: {str(e)}"
        )

@router.post("/{shop_id}")
async def save_onboarding_api(shop_id: str, data: dict):
    """
    온보딩 데이터 저장
    """
    success = save_onboarding_db(shop_id, data)

    if not success:
        raise HTTPException(status_code=500, detail="온보딩 데이터 저장 실패")

    return {
        "success": True,
        "message": "온보딩 데이터 저장 완료"
    }


@router.get("/{shop_id}")
async def get_onboarding_api(shop_id: str):
    """
    온보딩 데이터 조회
    """
    result = get_onboarding_db(shop_id)

    if not result:
        raise HTTPException(status_code=404, detail="온보딩 데이터 없음")

    return result