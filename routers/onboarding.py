from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional

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

@router.get("/{shop_id}")
async def get_onboarding(shop_id: str):
    return {"shop_id": shop_id, "status": "mock"}