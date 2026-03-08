from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
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

@router.post("")
async def save_onboarding(req: OnboardingRequest):
    return {"shop_id": req.shop_id, "status": "success"}

#@router.get("/{shop_id}")
async def get_onboarding(shop_id: str):
    return {"shop_id": shop_id, "status": "mock"}

@router.post("/{shop_id}")
def save_onboarding_api(shop_id: str, data: dict):
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
def get_onboarding_api(shop_id: str):
    """
    온보딩 데이터 조회
    """
    result = get_onboarding_db(shop_id)

    if not result:
        raise HTTPException(status_code=404, detail="온보딩 데이터 없음")

    return result