"""
에이전트 라우터
- POST /api/agent/run: 에이전트 파이프라인 실행
- POST /api/agent/review: 사장님 검토 결과 처리 (OK/수정/취소)
- GET /api/agent/posts/{shop_id}: 게시물 목록 조회
- POST /api/agent/save: 게시물 저장
- GET /api/agent/post/detail/{post_id}: 게시물 상세
- POST /api/agent/manual_chat: 사장님 GPT 실시간 대화 (스트리밍)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from services.cosmos_db import get_post_by_shop
from services.cosmos_db import save_draft
from services.cosmos_db import save_post_data
from services.cosmos_db import get_post_detail_data
from agents.orchestrator import run_pipeline
from routers.custom_chat import router as custom_chat_router  # 추가

router = APIRouter()

# 커스텀 라우터 결합
router.include_router(custom_chat_router)

# Request / Response 모델
class AgentRunRequest(BaseModel):
    shop_id: str
    trigger: str
    photo_ids: Optional[List[str]] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "trigger": "auto",
                "photo_ids": None
            }
        }

class AgentReviewRequest(BaseModel):
    shop_id: str                        
    post_id: str
    action: str                        
    edited_caption: Optional[str] = None  # action이 "edit"일 때만
    
    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "post_id": "post_abc12345",
                "action": "ok",
                "edited_caption": None
            }
        }

class PostSaveRequest(BaseModel):
    shop_id: str
    caption: str
    hashtags: List[str]
    photo_ids: List[str]
    cta: str
    status: str = "success"


# POST /api/agent/run
@router.post("/run")
async def agent_run(req: AgentRunRequest):
    """
    에이전트 파이프라인 실행
    
    트리거 타입:
    - auto: 예약 시간 자동 실행 (photo_ids 없으면 자동 선택)
    - manual: 사장님 직접 실행 (photo_ids 있으면 해당 사진 사용, 없으면 자동 선택)
    
    photo_ids가 없으면 orchestrator가 get_top_photos로 자동 선택합니다.
    """
    if req.trigger not in ("auto", "manual"):
        raise HTTPException(400, "trigger는 'auto' 또는 'manual'이어야 합니다.")

    # 수정: manual도 photo_ids 선택사항
    # orchestrator가 None이면 자동으로 후보 선택
    
    try:
        result = await run_pipeline(
            shop_id=req.shop_id,
            trigger=req.trigger,
            photo_ids=req.photo_ids  # None이어도 OK
        )
        return result
    except Exception as e:
        raise HTTPException(500, f"에이전트 실행 실패: {str(e)}")


# POST /api/agent/review
@router.post("/review")
async def agent_review(req: AgentReviewRequest):
    """
    사장님 검토 결과 처리
    - ok: 즉시 인스타 업로드
    - edit: 수정된 캡션으로 업로드
    - cancel: 업로드 중단
    """
    if req.action not in ("ok", "edit", "cancel"):
        raise HTTPException(400, "action은 'ok', 'edit', 'cancel' 중 하나여야 합니다.")

    if req.action == "edit" and not req.edited_caption:
        raise HTTPException(400, "edit 액션은 edited_caption이 필요합니다.")

    try:
        if req.action == "cancel":
            await _handle_cancel(req.shop_id, req.post_id)
            return {"post_id": req.post_id, "status": "cancelled"}

        caption_to_use = req.edited_caption if req.action == "edit" else None
        await _handle_upload(req.shop_id, req.post_id, caption_to_use)
        return {"post_id": req.post_id, "status": "uploaded"}

    except Exception as e:
        raise HTTPException(500, f"검토 처리 실패: {str(e)}")


# 1. 게시물 목록 조회 (대시보드용)
@router.get("/posts/{shop_id}")
async def get_posts(shop_id: str):
    posts = get_post_by_shop(shop_id)
    return {"posts": posts}


# 3. 게시물 최종 확정 및 저장
@router.post("/save")
async def save_post(req: PostSaveRequest):
    success = save_post_data(req.shop_id, req.dict())
    if not success:
        raise HTTPException(status_code=500, detail="게시물 저장 실패")
    return {"status": "success", "message": "게시물이 저장되었습니다."}


# 4. 게시물 상세 조회
@router.get("/post/detail/{post_id}")
async def get_post_detail(post_id: str, shop_id: str):
    post = get_post_detail_data(post_id, shop_id)
    if not post:
        raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")
    return post


# 내부 헬퍼
async def _handle_upload(shop_id: str, post_id: str, edited_caption: str = None):
    """초안 조회 → (캡션 수정) → Instagram 업로드 → 이력 저장"""
    from services.cosmos_db import get_draft, save_post_data

    draft = get_draft(shop_id=shop_id, post_id=post_id)
    if not draft:
        raise ValueError(f"초안을 찾을 수 없습니다: {post_id}")

    if edited_caption:
        draft["caption"] = edited_caption

    # TODO: 태경님 instagram 업로드 완성되면 주석 해제
    # from routers.instagram import upload_to_instagram
    # await upload_to_instagram(draft)

    save_post_data(
        shop_id=shop_id,
        post_data={
            "id": post_id,
            "caption": draft["caption"],
            "hashtags": draft.get("hashtags", []),
            "photo_ids": draft.get("photo_ids", []),
            "cta": draft.get("cta", ""),
            "status": "success"
        }
    )

    # 업로드 성공 후 캡션 임베딩 → Vector DB 저장 (RAG 품질 향상)
    try:
        from agents.rag_tool import get_embedding
        from services.vector_db import save_embedding

        caption_for_embed = draft["caption"]
        embedding = await get_embedding(caption_for_embed)
        if embedding:
            save_embedding(
                shop_id=shop_id,
                post_id=post_id,
                caption=caption_for_embed,
                embedding=embedding
            )
            print(f"[agent] AG-051 Vector DB 임베딩 저장 완료 → post_id={post_id}")
        else:
            print(f"[agent] AG-051 임베딩 생성 실패 → Vector DB 저장 스킵")
    except Exception as e:
        # 임베딩 저장 실패해도 업로드 자체는 성공으로 처리
        print(f"[agent] AG-051 Vector DB 저장 실패 (무시됨): {e}")


async def _handle_cancel(shop_id: str, post_id: str):
    """취소 처리 → 이력에 cancel 저장"""
    from services.cosmos_db import get_draft, save_post_data

    draft = get_draft(shop_id=shop_id, post_id=post_id)
    if draft:
        save_post_data(
            shop_id=shop_id,
            post_data={
                "id": post_id,
                "caption": draft.get("caption", ""),
                "hashtags": draft.get("hashtags", []),
                "photo_ids": draft.get("photo_ids", []),
                "cta": draft.get("cta", ""),
                "status": "cancel"
            }
        )