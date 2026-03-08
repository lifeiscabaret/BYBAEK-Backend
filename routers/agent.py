from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from services.cosmos_db import get_post_by_shop     #게시물 목록 조회(대시보드용)
from services.cosmos_db import save_draft           #게시물 생성 초안(AI 에이전트 실행)
from services.cosmos_db import save_post_data       #게시물 최종 확정 및 저장
from services.cosmos_db import get_post_detail_data #게시물 상세 조회
router = APIRouter()
# app.include_router(agent.router, prefix="/api/agent", tags=["Agent"])

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

class AgentRunRequest(BaseModel):
    shop_id: str
    trigger: str
    photo_ids: Optional[List[str]] = None

class PostSaveRequest(BaseModel): # 게시물 저장용
    shop_id: str
    caption: str
    hashtags: List[str]
    photo_ids: List[str]
    cta: str
    status: str = "success"

# 1. 게시물 목록 조회 (대시보드용)
@router.get("/posts/{shop_id}") # /api/agent/posts/3sesac18
async def get_posts(shop_id: str):
    posts = get_post_by_shop(shop_id)
    return {"posts": posts}

# 2. AI 에이전트 실행 (게시물 생성 초안)
# @router.post("/run")
# async def run_agent(req: AgentRunRequest):
#     # AI 로직
#     # 임시로 Mock 데이터를 반환하고 DB에 초안을 저장
#     post_id = f"post_{req.shop_id}_temp" 
#     caption = "방금 갓 자른 페이드컷! 이번 주말 예약 서두르세요."
#     hashtags = ["#바버샵", "#페이드컷", "#남자머리"]
    
#     # DB에 초안 저장
#     save_draft(req.shop_id, post_id, caption, hashtags, req.photo_ids or [], cta)
    
#     return {
#         "post_id": post_id,
#         "caption": caption,
#         "hashtags": hashtags,
#         "status": "pending"
#     }

# 3. 게시물 최종 확정 및 저장
@router.post("/save")
async def save_post(req: PostSaveRequest):
    success = save_post_data(req.shop_id, req.dict())
    if not success:
        raise HTTPException(status_code=500, detail="게시물 저장 실패")
    return {"status": "success", "message": "게시물이 저장되었습니다."}

# 4. 게시물 상세 조회
@router.get("/post/detail/{post_id}") # /api/agent/post/detail/post_123
async def get_post_detail(post_id: str, shop_id: str):
    post = get_post_detail_data(post_id, shop_id)
    if not post:
        raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")
    return post