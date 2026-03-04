from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()

class AgentRunRequest(BaseModel):
    shop_id: str
    trigger: str
    photo_ids: Optional[List[str]] = None

class AgentReviewRequest(BaseModel):
    post_id: str
    action: str
    edited_caption: Optional[str] = None

@router.post("/run")
async def run_agent(req: AgentRunRequest):
    return {
        "post_id": "post_mock_001",
        "caption": "테스트 캡션입니다.",
        "hashtags": ["#바버샵", "#페이드컷"],
        "photo_urls": ["https://blob.../test.jpg"],
        "cta": "DM으로 예약 문의주세요",
        "status": "draft"
    }

@router.post("/review")
async def review_post(req: AgentReviewRequest):
    if req.action == "cancel":
        return {"post_id": req.post_id, "status": "cancelled"}
    return {"post_id": req.post_id, "status": "uploaded"}