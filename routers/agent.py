"""
에이전트 라우터
- POST /api/agent/run: 에이전트 파이프라인 실행
- POST /api/agent/review: 사장님 검토 결과 처리 (OK/수정/취소)
- GET /api/agent/posts/{shop_id}: 게시물 목록 조회
- POST /api/agent/save: 게시물 저장
- GET /api/agent/post/detail/{post_id}: 게시물 상세
"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from services.cosmos_db import get_post_by_shop
from services.cosmos_db import save_draft
from services.cosmos_db import save_post_data
from services.cosmos_db import get_post_detail_data
from auth.token_verify import (
    get_current_shop,
    get_current_shop_or_review,
    require_shop_owner,
    require_post_access,
)
from routers.photos import _to_sas_url
from orchestrator_v2 import run_pipeline

router = APIRouter()

class AgentRunRequest(BaseModel):
    shop_id: str
    trigger: str
    photo_ids: Optional[List[str]] = None
    message: Optional[str] = None   # 사장님 직접 요청 (manual 트리거 시)
    photo_intent: Optional[str] = "haircut"  # [v2] "haircut" | "shop_intro"
    # [v2.1] 비포/애프터 쌍 (manual 전용, 정확히 2개 photo_id). auto 트리거는 대상 아님.
    before_after_pair_ids: Optional[List[str]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "trigger": "auto",
                "photo_ids": None,
                "message": None,
                "photo_intent": "haircut",
                "before_after_pair_ids": None
            }
        }

class AgentReviewRequest(BaseModel):
    shop_id: str
    post_id: str
    action: str
    edited_caption: Optional[str] = None
    # 검토 화면에서 사진을 갈아끼우거나 순서를 바꾼 경우. None 이면 초안 사진 그대로.
    edited_photo_ids: Optional[List[str]] = None

    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "post_id": "post_abc12345",
                "action": "ok",
                "edited_caption": None,
                "edited_photo_ids": None
            }
        }

class PostSaveRequest(BaseModel):
    shop_id: str
    caption: str
    hashtags: List[str]
    photo_ids: List[str]
    cta: str
    status: str = "success"


@router.post("/run")
async def agent_run(req: AgentRunRequest, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, req.shop_id)
    if req.trigger not in ("auto", "manual"):
        raise HTTPException(400, "trigger는 'auto' 또는 'manual'이어야 합니다.")

    # [v2.1] 비포/애프터 검증: manual 전용 + 정확히 2개 photo_id
    before_after_pair_ids = req.before_after_pair_ids
    if before_after_pair_ids:
        if req.trigger != "manual":
            raise HTTPException(400, "before_after_pair_ids는 manual 트리거에서만 사용할 수 있습니다.")
        if len(before_after_pair_ids) != 2:
            raise HTTPException(400, "before_after_pair_ids는 정확히 2개의 photo_id여야 합니다.")

    try:
        result = await run_pipeline(
            shop_id=req.shop_id,
            trigger=req.trigger,
            photo_ids=req.photo_ids,
            message=req.message,
            photo_intent=req.photo_intent or "haircut",
            before_after_pair_ids=before_after_pair_ids
        )
        # 프론트로 내려가는 photo_urls는 비공개 컨테이너 대비 SAS로 래핑
        if isinstance(result, dict) and result.get("photo_urls"):
            result["photo_urls"] = [_to_sas_url(u) for u in result["photo_urls"] if u]
        return result
    except Exception as e:
        raise HTTPException(500, f"에이전트 실행 실패: {str(e)}")


@router.post("/review")
async def agent_review(
    req: AgentReviewRequest,
    current_shop: dict = Depends(get_current_shop_or_review),
):
    # 메일 검토 링크 토큰으로도 들어올 수 있으므로, 샵 소유권 + 대상 post_id 까지 검증한다.
    require_post_access(current_shop, req.shop_id, req.post_id)
    if req.action not in ("ok", "edit", "cancel"):
        raise HTTPException(400, "action은 'ok', 'edit', 'cancel' 중 하나여야 합니다.")

    if req.action == "edit" and not req.edited_caption:
        raise HTTPException(400, "edit 액션은 edited_caption이 필요합니다.")

    try:
        if req.action == "cancel":
            await _handle_cancel(req.shop_id, req.post_id)
            return {"post_id": req.post_id, "status": "cancelled"}

        caption_to_use = req.edited_caption if req.action == "edit" else None
        await _handle_upload(
            req.shop_id, req.post_id, caption_to_use,
            edited_photo_ids=req.edited_photo_ids,
            review_action=req.action,
        )
        return {"post_id": req.post_id, "status": "uploaded"}

    except Exception as e:
        raise HTTPException(500, f"검토 처리 실패: {str(e)}")


@router.get("/posts/{shop_id}")
async def get_posts(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    posts = get_post_by_shop(shop_id)
    # 썸네일은 비공개 컨테이너 대비 SAS로 래핑 (placeholder.com 등 외부 URL은 그대로)
    for p in posts:
        t = p.get("thumbnail_url")
        if t and "blob.core.windows.net" in t:
            p["thumbnail_url"] = _to_sas_url(t)
    return {"posts": posts}


@router.post("/save")
async def save_post(req: PostSaveRequest, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, req.shop_id)
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


@router.get("/post/detail/{post_id}")
async def get_post_detail(
    post_id: str,
    shop_id: str,
    current_shop: dict = Depends(get_current_shop_or_review),
):
    # /review 가 메일 링크 토큰만 들고 초안을 불러오는 경로. 대상 post_id 까지 검증한다.
    # (get_post_detail_data 는 read_item 직접 조회라 status='pending' 인 초안도 나온다.)
    require_post_access(current_shop, shop_id, post_id)
    post = get_post_detail_data(post_id, shop_id)
    if not post:
        raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")
    # 비공개 컨테이너 대비 blob URL들을 SAS로 래핑
    for pd in post.get("photo_details", []):
        if pd.get("blob_url"):
            pd["blob_url"] = _to_sas_url(pd["blob_url"])
    if post.get("photo_urls"):
        post["photo_urls"] = [_to_sas_url(u) for u in post["photo_urls"] if u]
    t = post.get("thumbnail_url")
    if t and "blob.core.windows.net" in t:
        post["thumbnail_url"] = _to_sas_url(t)
    return post


async def _handle_upload(
    shop_id: str,
    post_id: str,
    edited_caption: str = None,
    edited_photo_ids: list = None,
    review_action: str = "ok",
):
    """초안 조회 → (캡션/사진 수정) → Instagram 업로드 → 이력 저장"""
    from services.cosmos_db import get_draft, save_post_data

    draft = get_draft(shop_id=shop_id, post_id=post_id)
    print(f"[DEBUG] draft 조회 결과: {draft}")
    if not draft:
        raise ValueError(f"초안을 찾을 수 없습니다: {post_id}")

    # AI 원본 캡션은 덮어쓰기 전에 따로 챙긴다. 재검토로 두 번 들어와도 최초 원본이
    # 유지되도록, 이미 ai_caption 이 있으면 그걸 그대로 쓴다.
    ai_caption = draft.get("ai_caption") or draft.get("caption", "")

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

    # 검토 화면에서 사진을 바꿨으면 그 목록(순서 포함)을 쓴다. None 이면 초안 그대로.
    photo_ids = edited_photo_ids if edited_photo_ids is not None else draft.get("photo_ids", [])

    # Instagram(외부)이 직접 fetch하므로, 비공개 컨테이너 대비 SAS URL로 전달한다.
    # (예전엔 split("?")[0]로 SAS를 벗겨 bare URL을 넘겼고, 공개 컨테이너에 의존했음)
    from services.cosmos_db import get_photo_by_id
    image_urls = []
    for pid in photo_ids:
        photo = get_photo_by_id(shop_id, pid)
        if photo and photo.get("blob_url"):
            sas_url = _to_sas_url(photo["blob_url"])
            image_urls.append(sas_url)

    print(f"[DEBUG] 최종 image_urls={image_urls}")
    print(f"[DEBUG] 업로드 조건: user={bool(insta_user_id)}, token={bool(access_token)}, urls={bool(image_urls)}")

    instagram_media_id = None
    if insta_user_id and access_token and image_urls:
        from routers.instagram import publish_photos
        instagram_media_id = await publish_photos(insta_user_id, access_token, image_urls, full_caption)
        print(f"[agent] 인스타 업로드 성공 → media_id={instagram_media_id}")
    else:
        raise ValueError(f"업로드 조건 미충족: user={bool(insta_user_id)}, token={bool(access_token)}, urls={bool(image_urls)}")

    save_post_data(
        shop_id=shop_id,
        post_data={
            "id":                 post_id,
            "caption":            caption,
            # AI 원본. 사장님이 고친 caption 과 나란히 남겨야 "원본 vs 수정본" 비교가 가능하다.
            "ai_caption":         ai_caption,
            "hashtags":           hashtags,
            "photo_ids":          photo_ids,
            "cta":                cta,
            "status":             "success" if instagram_media_id else "fail",
            "review_action":      review_action,
            "instagram_media_id": instagram_media_id,
            "published_at":       datetime.now(timezone.utc).isoformat(),
        }
    )

    if instagram_media_id:
        from agents.rag_tool import index_post_for_rag
        await index_post_for_rag(
            shop_id=shop_id, post_id=post_id,
            caption=caption, hashtags=hashtags, cta=cta
        )


async def _handle_cancel(shop_id: str, post_id: str):
    from services.cosmos_db import get_draft, save_post_data
    draft = get_draft(shop_id=shop_id, post_id=post_id)
    if draft:
        save_post_data(
            shop_id=shop_id,
            post_data={
                "id":        post_id,
                "caption":   draft.get("caption", ""),
                "hashtags":  draft.get("hashtags", []),
                "photo_ids": draft.get("photo_ids", []),
                "cta":       draft.get("cta", ""),
                "status":    "cancel",
                "review_action": "cancel"
            }
        )


@router.get("/metrics/{shop_id}")
async def get_agent_metrics(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    try:
        from services.cosmos_db import get_cosmos_container
        container = get_cosmos_container("Post")
        query = "SELECT c.metrics FROM c WHERE c.shop_id = @shop_id AND IS_DEFINED(c.metrics)"
        parameters = [{"name": "@shop_id", "value": shop_id}]
        items = list(container.query_items(query=query, parameters=parameters,
                                           enable_cross_partition_query=True))

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
            "total_posts":        total,
            "avg_caption_score":  avg_score,
            "avg_retry_count":    round(sum(retries) / total, 2),
            "retry_rate":         retry_rate,
            "model_distribution": models,
            "score_distribution": score_dist
        }

    except Exception as e:
        raise HTTPException(500, str(e))