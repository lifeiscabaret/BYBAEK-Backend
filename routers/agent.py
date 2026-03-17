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
    shop_id: str                        # get_draft에 필수
    post_id: str
    action: str                         # "ok" | "edit" | "cancel"
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

    # 인스타 인증 정보 조회
    from services.cosmos_db import get_auth
    shop_auth      = get_auth(shop_id) or {}
    insta_user_id  = shop_auth.get("insta_user_id")
    access_token   = shop_auth.get("insta_access_token")

    # 캡션 구성 (caption + hashtags + cta)
    caption  = draft["caption"]
    hashtags = draft.get("hashtags", [])
    cta      = draft.get("cta", "")
    full_caption = f"{caption}\n\n{' '.join(hashtags)}\n{cta}".strip()

    # blob_url 리스트 조회
    from services.cosmos_db import get_photo_by_id
    photo_ids  = draft.get("photo_ids", [])
    image_urls = []
    for pid in photo_ids:
        photo = get_photo_by_id(shop_id, pid)
        if photo and photo.get("blob_url"):
            image_urls.append(photo["blob_url"])

    # Instagram 업로드
    instagram_media_id = None
    if insta_user_id and access_token and image_urls:
        try:
            from routers.instagram import create_image_container, create_carousel_container, publish_container
            container_ids  = [create_image_container(insta_user_id, access_token, url) for url in image_urls]
            creation_id    = create_carousel_container(insta_user_id, access_token, container_ids, full_caption)
            instagram_media_id = publish_container(insta_user_id, creation_id, access_token)
            print(f"[agent] 인스타 업로드 성공 → media_id={instagram_media_id}")
        except Exception as e:
            print(f"[agent] 인스타 업로드 실패: {e} → status=fail 로 저장")

    save_post_data(
        shop_id=shop_id,
        post_data={
            "id":                  post_id,
            "caption":             caption,
            "hashtags":            hashtags,
            "photo_ids":           photo_ids,
            "cta":                 cta,
            "status":              "success" if instagram_media_id else "fail",
            "instagram_media_id":  instagram_media_id
        }
    )

    # RAG 플라이휠: 업로드 성공한 캡션을 Vector DB에 자동 저장
    # → 쓸수록 RAG 품질이 올라가는 구조
    if instagram_media_id:
        try:
            from agents.rag_tool import get_embedding
            from services.vector_db import save_embedding
            full_text = f"{caption} {' '.join(hashtags)} {cta}".strip()
            embedding = await get_embedding(full_text)
            if embedding:
                save_embedding(shop_id, post_id, full_text, embedding)
                print(f"[agent] RAG 플라이휠: 업로드 성공 캡션 Vector DB 저장 완료 → {post_id}")
        except Exception as e:
            print(f"[agent] RAG 플라이휠 저장 실패 (무시): {e}")


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