"""
에이전트 라우터
- POST /api/agent/run: 에이전트 파이프라인 실행
- POST /api/agent/review: 사장님 검토 결과 처리 (OK/수정/취소)
- GET /api/agent/posts/{shop_id}: 게시물 목록 조회
- POST /api/agent/save: 게시물 저장
- GET /api/agent/post/detail/{post_id}: 게시물 상세
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from services.cosmos_db import get_post_by_shop
from services.cosmos_db import save_draft
from services.cosmos_db import save_post_data
from services.cosmos_db import get_post_detail_data
from agents.orchestrator import run_pipeline

router = APIRouter()

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
    action: str                          # "ok" | "edit" | "cancel"
    edited_caption: Optional[str] = None # action이 "edit"일 때만

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
    if req.trigger not in ("auto", "manual"):
        raise HTTPException(400, "trigger는 'auto' 또는 'manual'이어야 합니다.")
    try:
        result = await run_pipeline(
            shop_id=req.shop_id,
            trigger=req.trigger,
            photo_ids=req.photo_ids
        )
        return result
    except Exception as e:
        raise HTTPException(500, f"에이전트 실행 실패: {str(e)}")


# POST /api/agent/review
@router.post("/review")
async def agent_review(req: AgentReviewRequest):
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


# GET /api/agent/posts/{shop_id}
@router.get("/posts/{shop_id}")
async def get_posts(shop_id: str):
    posts = get_post_by_shop(shop_id)
    return {"posts": posts}


# POST /api/agent/save
@router.post("/save")
async def save_post(req: PostSaveRequest):
    import uuid
    post_id = f"post_{uuid.uuid4().hex[:8]}"
    save_draft(
        shop_id=req.shop_id,
        post_id=post_id,
        caption=req.caption,
        hashtags=req.hashtags,
        photo_ids=req.photo_ids,
        cta=req.cta,
        review_action="pending"
    )
    return {"status": "success", "post_id": post_id}


# GET /api/agent/post/detail/{post_id}
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
    print(f"[DEBUG] draft 조회 결과: {draft}")
    if not draft:
        raise ValueError(f"초안을 찾을 수 없습니다: {post_id}")

    if edited_caption:
        draft["caption"] = edited_caption

    from services.cosmos_db import get_auth
    shop_auth     = get_auth(shop_id) or {}
    insta_user_id = shop_auth.get("insta_user_id")
    access_token  = shop_auth.get("insta_access_token")
    print(f"[DEBUG] insta_user_id={insta_user_id}, token_exists={bool(access_token)}")

    caption      = draft["caption"]
    hashtags     = draft.get("hashtags", [])
    cta          = draft.get("cta", "")
    full_caption = f"{caption}\n\n{' '.join(hashtags)}\n{cta}".strip()

    from services.cosmos_db import get_photo_by_id
    photo_ids  = draft.get("photo_ids", [])
    image_urls = []
    for pid in photo_ids:
        photo = get_photo_by_id(shop_id, pid)
        print(f"[DEBUG] photo_id={pid}, photo={photo}")
        if photo and photo.get("blob_url"):
            image_urls.append(photo["blob_url"])

    print(f"[DEBUG] 최종 image_urls={image_urls}")
    print(f"[DEBUG] 업로드 조건: user={bool(insta_user_id)}, token={bool(access_token)}, urls={bool(image_urls)}")

    # Instagram 업로드 (1장: 단일, 2장+: CAROUSEL 자동 분기)
    instagram_media_id = None
    if insta_user_id and access_token and image_urls:
        from routers.instagram import publish_photos
        instagram_media_id = publish_photos(insta_user_id, access_token, image_urls, full_caption)
        print(f"[agent] 인스타 업로드 성공 → media_id={instagram_media_id}")
    else:
        raise ValueError(f"업로드 조건 미충족: user={bool(insta_user_id)}, token={bool(access_token)}, urls={bool(image_urls)}")

    save_post_data(
        shop_id=shop_id,
        post_data={
            "id":                 post_id,
            "caption":            caption,
            "hashtags":           hashtags,
            "photo_ids":          photo_ids,
            "cta":                cta,
            "status":             "success" if instagram_media_id else "fail",
            "instagram_media_id": instagram_media_id
        }
    )

    # RAG 플라이휠: 업로드 성공한 캡션 Vector DB 저장
    if instagram_media_id:
        try:
            from agents.rag_tool import get_embedding
            from services.vector_db import save_embedding
            full_text = f"{caption} {' '.join(hashtags)} {cta}".strip()
            embedding = await get_embedding(full_text)
            if embedding:
                save_embedding(shop_id, post_id, full_text, embedding)
                print(f"[agent] RAG 플라이휠: Vector DB 저장 완료 → {post_id}")
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
                "id":       post_id,
                "caption":  draft.get("caption", ""),
                "hashtags": draft.get("hashtags", []),
                "photo_ids":draft.get("photo_ids", []),
                "cta":      draft.get("cta", ""),
                "status":   "cancel"
            }
        )


@router.get("/metrics/{shop_id}")
async def get_agent_metrics(shop_id: str):
    try:
        from services.cosmos_db import get_cosmos_container
        container = get_cosmos_container("Post")
        query = f"SELECT c.metrics FROM c WHERE c.shop_id = '{shop_id}' AND IS_DEFINED(c.metrics)"
        items = list(container.query_items(query=query, enable_cross_partition_query=True))

        if not items:
            return {
                "total_posts": 0,
                "avg_caption_score": 0,
                "retry_rate": "0%",
                "model_distribution": {},
                "message": "아직 데이터가 없습니다. 에이전트를 실행해주세요."
            }

        scores, retries, models = [], [], {}
        for item in items:
            m = item.get("metrics", {})
            scores.append(m.get("caption_score", 0))
            retries.append(m.get("retry_count", 0))
            model = m.get("model_used", "unknown")
            models[model] = models.get(model, 0) + 1

        total      = len(scores)
        avg_score  = round(sum(scores) / total, 2)
        retry_rate = f"{round(len([r for r in retries if r > 0]) / total * 100)}%"
        score_dist = {
            "0.9+":    len([s for s in scores if s >= 0.9]),
            "0.8~0.9": len([s for s in scores if 0.8 <= s < 0.9]),
            "0.7~0.8": len([s for s in scores if 0.7 <= s < 0.8]),
            "0.7미만":  len([s for s in scores if s < 0.7]),
        }

        return {
            "total_posts":       total,
            "avg_caption_score": avg_score,
            "avg_retry_count":   round(sum(retries) / total, 2),
            "retry_rate":        retry_rate,
            "model_distribution":models,
            "score_distribution":score_dist
        }

    except Exception as e:
        raise HTTPException(500, str(e))