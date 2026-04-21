"""
BYBAEK Orchestrator v2 — LangGraph StateGraph 기반
"""
import requests
from io import BytesIO
import os
import json
import asyncio
import uuid
from typing import TypedDict, Literal, Optional
from PIL import Image

from langgraph.graph import StateGraph, END
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory

from agents.web_search import web_search_agent
from agents.photo_select import photo_select_agent
from agents.post_writer import post_writer_agent
from agents.rag_tool import search_rag_context
from agents.performance_feedback import node_fetch_performance, inject_performance_to_rag

QUALITY_THRESHOLD = 0.7
MAX_RETRY         = 2
MIN_PHOTO_COUNT   = 1


class PostState(TypedDict):
    shop_id:          str
    trigger:          str
    photo_ids:        Optional[list]
    performance_history: dict
    tier:             str
    trend_data:       dict
    brand_settings:   dict
    photo_candidates: list
    recent_posts:     list
    trend_score:      float
    trend_retries:    int
    selected_photos:  list
    rag_context:      dict
    post_draft:       dict
    caption_score:    float
    caption_retries:  int
    post_id:          str
    status:           str


async def node_classify(state: PostState) -> PostState:
    photo_ids = state.get("photo_ids") or []
    trigger   = state.get("trigger", "auto")
    if trigger == "manual":
        tier, reason = "full", "manual 트리거 (사장님 직접 개입)"
    elif len(photo_ids) >= 5:
        tier, reason = "full", f"사진 {len(photo_ids)}장 대량 처리"
    else:
        tier, reason = "mini", "일반 자동 실행"
    print(f"[orchestrator_v2] node_classify → tier={tier} ({reason})")
    return {**state, "tier": tier}


async def node_fetch_data(state: PostState) -> PostState:
    print(f"[orchestrator_v2] node_fetch_data → shop_id={state['shop_id']}")
    trend_data, brand_settings, photo_candidates, recent_posts = await asyncio.gather(
        web_search_agent(state["shop_id"]),
        _get_brand_settings(state["shop_id"]),
        _get_photo_candidates(state["shop_id"]),
        _get_recent_posts(state["shop_id"])
    )
    return {
        **state,
        "trend_data":       trend_data,
        "brand_settings":   brand_settings,
        "photo_candidates": photo_candidates,
        "recent_posts":     recent_posts,
        "trend_retries":    state.get("trend_retries", 0),
    }


async def node_evaluate_trend(state: PostState) -> PostState:
    kernel = _init_kernel(state["tier"])
    score  = await _evaluate_trend(kernel, state["trend_data"])
    print(f"[orchestrator_v2] node_evaluate_trend → score={score:.2f}, retries={state.get('trend_retries', 0)}")
    return {**state, "trend_score": score}


async def node_retry_trend(state: PostState) -> PostState:
    retries    = state.get("trend_retries", 0) + 1
    trend_data = await web_search_agent(state["shop_id"], force_refresh=True)
    print(f"[orchestrator_v2] node_retry_trend → 재시도 {retries}/{MAX_RETRY}")
    return {**state, "trend_data": trend_data, "trend_retries": retries}


async def node_select_photos(state: PostState) -> PostState:
    trigger   = state["trigger"]
    photo_ids = state.get("photo_ids") or []
    if trigger == "manual" and photo_ids:
        print(f"[orchestrator_v2] node_select_photos → manual, {len(photo_ids)}장")
        selected = await _get_photos_by_ids(state["shop_id"], photo_ids)
    else:
        selected = await photo_select_agent(
            shop_id=state["shop_id"],
            trend_data=state["trend_data"],
            photo_candidates=state["photo_candidates"],
            brand_settings=state["brand_settings"]
        )
        if len(selected) < MIN_PHOTO_COUNT:
            print(f"[orchestrator_v2] 사진 부족 ({len(selected)}장) → 날짜 확장")
            extended = await _get_photo_candidates(state["shop_id"], extend_days=30)
            selected = await photo_select_agent(
                shop_id=state["shop_id"],
                trend_data=state["trend_data"],
                photo_candidates=extended,
                brand_settings=state["brand_settings"]
            )
    print(f"[orchestrator_v2] node_select_photos → {len(selected)}장 선택")
    return {**state, "selected_photos": selected}


async def node_search_rag(state: PostState) -> PostState:
    print(f"[orchestrator_v2] node_search_rag → 시작")
    rag_context = await search_rag_context(
        shop_id=state["shop_id"],
        trend_data=state["trend_data"],
        selected_photos=state["selected_photos"],
        brand_settings=state["brand_settings"],
        recent_posts=state["recent_posts"]
    )
    rag_context = await inject_performance_to_rag(rag_context, state.get("performance_history", {}))
    return {**state, "rag_context": rag_context}


async def node_write_post(state: PostState) -> PostState:
    kernel  = _init_kernel(state["tier"])
    retries = state.get("caption_retries", 0)
    previous_draft = state.get("post_draft") if retries > 0 else None
    feedback = (
        f"브랜드 톤 점수 {state.get('caption_score', 0):.2f} 미달. 금칙어 제거 및 톤 재조정 필요."
        if previous_draft else None
    )
    post_draft = await post_writer_agent(
        shop_id=state["shop_id"],
        trend_data=state["trend_data"],
        selected_photos=state["selected_photos"],
        brand_settings=state["brand_settings"],
        recent_posts=state["recent_posts"],
        rag_context=state["rag_context"],
        previous_draft=previous_draft,
        feedback=feedback
    )
    caption_score = await _evaluate_caption(kernel, post_draft, state["brand_settings"])
    print(f"[orchestrator_v2] node_write_post → score={caption_score:.2f}, retries={retries}")
    return {**state, "post_draft": post_draft, "caption_score": caption_score}


async def node_upgrade_model(state: PostState) -> PostState:
    print(f"[orchestrator_v2] node_upgrade_model → mini → full 승격")
    return {**state, "tier": "full", "caption_retries": 0}


async def node_increment_caption_retry(state: PostState) -> PostState:
    retries = state.get("caption_retries", 0) + 1
    return {**state, "caption_retries": retries}


async def node_save_draft(state: PostState) -> PostState:
    post_id = f"post_{uuid.uuid4().hex[:8]}"
    await _save_draft(
        shop_id=state["shop_id"],
        post_id=post_id,
        post_draft=state["post_draft"],
        selected_photos=state["selected_photos"],
        caption_score=state["caption_score"],
        retry_count=state.get("caption_retries", 0),
        model_used=state["tier"]
    )
    need_review = state["brand_settings"].get("insta_review_bfr_upload_yn", True)
    if not need_review:
        await _auto_upload_instagram(
            state["shop_id"], post_id,
            state["post_draft"], state["selected_photos"]
        )
    await _send_push_notification(state["shop_id"], post_id, state["post_draft"])
    print(f"[orchestrator_v2] node_save_draft → post_id={post_id}")
    return {**state, "post_id": post_id, "status": "draft"}


def route_after_trend_eval(state: PostState) -> Literal["retry_trend", "select_photos"]:
    if state["trend_score"] < QUALITY_THRESHOLD and state.get("trend_retries", 0) < MAX_RETRY:
        return "retry_trend"
    return "select_photos"


def route_after_write(state: PostState) -> Literal["increment_retry", "upgrade_model", "save_draft"]:
    score   = state.get("caption_score", 0)
    retries = state.get("caption_retries", 0)
    tier    = state.get("tier", "mini")
    if score >= QUALITY_THRESHOLD:
        return "save_draft"
    if retries < MAX_RETRY:
        return "increment_retry"
    if tier == "mini":
        return "upgrade_model"
    return "save_draft"


def build_graph() -> StateGraph:
    graph = StateGraph(PostState)
    graph.add_node("classify",          node_classify)
    graph.add_node("fetch_data",        node_fetch_data)
    graph.add_node("evaluate_trend",    node_evaluate_trend)
    graph.add_node("retry_trend",       node_retry_trend)
    graph.add_node("select_photos",     node_select_photos)
    graph.add_node("search_rag",        node_search_rag)
    graph.add_node("write_post",        node_write_post)
    graph.add_node("increment_retry",   node_increment_caption_retry)
    graph.add_node("upgrade_model",     node_upgrade_model)
    graph.add_node("save_draft",        node_save_draft)
    graph.add_node("fetch_performance", node_fetch_performance)

    graph.set_entry_point("classify")
    graph.add_edge("classify",          "fetch_data")
    graph.add_edge("fetch_data",        "fetch_performance")
    graph.add_edge("fetch_performance", "evaluate_trend")
    graph.add_edge("retry_trend",       "evaluate_trend")
    graph.add_edge("select_photos",     "search_rag")
    graph.add_edge("search_rag",        "write_post")
    graph.add_edge("increment_retry",   "write_post")
    graph.add_edge("upgrade_model",     "write_post")
    graph.add_edge("save_draft",        END)

    graph.add_conditional_edges("evaluate_trend", route_after_trend_eval,
        {"retry_trend": "retry_trend", "select_photos": "select_photos"})
    graph.add_conditional_edges("write_post", route_after_write,
        {"increment_retry": "increment_retry", "upgrade_model": "upgrade_model", "save_draft": "save_draft"})

    return graph.compile()


async def run_pipeline(shop_id: str, trigger: str, photo_ids: list = None) -> dict:
    app = build_graph()
    initial_state: PostState = {
        "shop_id":           shop_id,
        "trigger":           trigger,
        "photo_ids":         photo_ids or [],
        "tier":              "mini",
        "trend_data":        {},
        "brand_settings":    {},
        "photo_candidates":  [],
        "recent_posts":      [],
        "trend_score":       0.0,
        "trend_retries":     0,
        "selected_photos":   [],
        "rag_context":       {},
        "post_draft":        {},
        "caption_score":     0.0,
        "caption_retries":   0,
        "post_id":           "",
        "status":            "running",
        "performance_history": {}
    }
    print(f"[orchestrator_v2] 파이프라인 시작 → shop_id={shop_id}, trigger={trigger}")
    final_state = await app.ainvoke(initial_state)
    return {
        "post_id":    final_state["post_id"],
        "caption":    final_state["post_draft"].get("caption", ""),
        "hashtags":   final_state["post_draft"].get("hashtags", []),
        "photo_urls": [p.get("blob_url") for p in final_state["selected_photos"]],
        "cta":        final_state["post_draft"].get("cta", ""),
        "status":     final_state["status"],
        "quality": {
            "trend_score":   final_state["trend_score"],
            "caption_score": final_state["caption_score"],
            "retries":       final_state["caption_retries"],
            "model_used":    final_state["tier"]
        }
    }


def _get_deployment_name(tier: str) -> str:
    if tier == "mini":
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    else:
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not deployment:
        raise ValueError("Azure OpenAI 배포 이름이 설정되지 않았습니다.")
    return deployment


def _init_kernel(tier: str = "mini") -> Kernel:
    deployment = _get_deployment_name(tier)
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel


async def _evaluate_trend(kernel: Kernel, trend_data: dict) -> float:
    try:
        chat_history = ChatHistory()
        chat_history.add_user_message(
            f"""트렌드 데이터 품질을 0.0~1.0으로 평가해줘.
[트렌드 데이터]
{json.dumps(trend_data, ensure_ascii=False, indent=2)[:800]}

기준:
- 최신성 (오늘 날짜 관련성)
- 구체성 (키워드 명확성)
- 바버샵 관련성

숫자 하나만 반환:
{{"score": 0.0~1.0}}"""
        )
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )
        raw = str(response).strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return float(result.get("score", 0.5))
    except Exception as e:
        print(f"[orchestrator_v2] 트렌드 평가 실패: {e}")
        return 0.5


async def _evaluate_caption(kernel: Kernel, post_draft: dict, brand_settings: dict) -> float:
    try:
        chat_history = ChatHistory()
        chat_history.add_user_message(
            f"""인스타 캡션 품질을 5개 항목으로 평가해줘.

[캡션]
{post_draft.get('caption', '')}

[해시태그]
{' '.join(post_draft.get('hashtags', []))}

[CTA]
{post_draft.get('cta', '')}

[브랜드 톤]
{brand_settings.get('brand_tone', '')}

[금칙어]
{brand_settings.get('forbidden_words', [])}

평가 항목 (각 0.0~1.0):
1. reservation_inquiry: 예약 문의로 이어질 가능성 (가중치 40%)
2. fade_keyword: 페이드컷 등 핵심 키워드 포함 (25%)
3. cta_strength: CTA 강도 (15%)
4. brand_tone: 브랜드 톤 일치 (10%)
5. target_appeal: 20-40대 남성 공감도 (10%)

JSON만 반환:
{{
  "reservation_inquiry": 0.0~1.0,
  "fade_keyword": 0.0~1.0,
  "cta_strength": 0.0~1.0,
  "brand_tone": 0.0~1.0,
  "target_appeal": 0.0~1.0
}}"""
        )
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )
        raw = str(response).strip().replace("```json", "").replace("```", "").strip()
        scores = json.loads(raw)
        total = (
            scores.get("reservation_inquiry", 0.5) * 0.40 +
            scores.get("fade_keyword",         0.5) * 0.25 +
            scores.get("cta_strength",         0.5) * 0.15 +
            scores.get("brand_tone",           0.5) * 0.10 +
            scores.get("target_appeal",        0.5) * 0.10
        )
        print(f"[orchestrator_v2] 캡션 평가 → 예약문의:{scores.get('reservation_inquiry',0):.2f} 페이드:{scores.get('fade_keyword',0):.2f} CTA:{scores.get('cta_strength',0):.2f}")
        return max(0.0, min(1.0, total))
    except Exception as e:
        print(f"[orchestrator_v2] 캡션 평가 실패: {e}")
        return 0.5


async def _get_brand_settings(shop_id: str) -> dict:
    from services.cosmos_db import get_onboarding
    data = get_onboarding(shop_id)
    if not data:
        return {
            "brand_tone":      "친근하고 편안한 말투",
            "forbidden_words": ["저렴", "할인"],
            "cta":             "DM으로 예약 문의주세요",
            "photo_range":     {"min": 1, "max": 5}
        }
    def to_list(val):
        if isinstance(val, list): return val
        if isinstance(val, str) and val: return [v.strip() for v in val.split(",")]
        return []
    shop = data.get("shop_info", {})
    return {
        "brand_tone":                 shop.get("brand_tone", "친근하고 편안한 말투"),
        "forbidden_words":            to_list(shop.get("forbidden_words")),
        "preferred_styles":           to_list(shop.get("preferred_styles")),
        "cta":                        shop.get("cta", "DM으로 예약 문의주세요"),
        "photo_range":                {"min": 1, "max": 5},
        "feed_style":                 shop.get("feed_style", {}),
        "brand_differentiation":      shop.get("shop_intro", ""),
        "insta_review_bfr_upload_yn": str(shop.get("insta_review_bfr_upload_yn", "Y")).upper() != "N"
    }


async def _get_photo_candidates(shop_id: str, extend_days: int = 0) -> list:
    from services.cosmos_db import get_top_photos
    limit = 20 if extend_days == 0 else 40
    return get_top_photos(shop_id, limit=limit)


async def _get_recent_posts(shop_id: str) -> list:
    from services.cosmos_db import get_recent_posts
    return get_recent_posts(shop_id, limit=3)


async def _get_photos_by_ids(shop_id: str, photo_ids: list) -> list:
    from services.cosmos_db import get_all_photos_by_shop
    all_photos = get_all_photos_by_shop(shop_id)
    return [p for p in all_photos if p.get("id") in photo_ids]


async def _save_draft(shop_id, post_id, post_draft, selected_photos,
                      caption_score=0.0, retry_count=0, model_used="mini"):
    from services.cosmos_db import save_draft
    save_draft(
        shop_id=shop_id, post_id=post_id,
        caption=post_draft.get("caption", ""),
        hashtags=post_draft.get("hashtags", []),
        photo_ids=[p.get("id", p.get("photo_id")) for p in selected_photos],
        cta=post_draft.get("cta", ""),
        review_action="pending",
        caption_score=round(caption_score, 2),
        retry_count=retry_count,
        model_used=model_used
    )


async def _auto_upload_instagram(shop_id, post_id, post_draft, selected_photos):
    try:
        from services.cosmos_db import get_auth, save_post_data
        shop_auth = get_auth(shop_id)
        if not shop_auth:
            return False

        insta_user_id      = shop_auth.get("insta_user_id")
        insta_access_token = shop_auth.get("insta_access_token")
        if not insta_user_id or not insta_access_token:
            return False

        caption      = post_draft.get("caption", "")
        hashtags     = post_draft.get("hashtags", [])
        cta          = post_draft.get("cta", "")
        full_caption = (caption + "\n\n" + " ".join(hashtags) + "\n" + cta).strip()

        # [현재] Blob Storage 공개 설정 → blob_url 직접 사용 (SAS 파라미터 제거)
        image_urls = [
            p["blob_url"].split("?")[0]
            for p in selected_photos if p.get("blob_url")
        ]

        # Blob 비공개 전환 시 아래 proxy 방식으로 교체 -> proxy 방식 실패, 다시 blo으로 전환
        # from routers.photos import get_proxy_url
        # image_urls = [get_proxy_url(p.get("id"), shop_id) for p in selected_photos if p.get("id")]

        if not image_urls:
            print("[orchestrator_v2] 업로드할 이미지 없음")
            return False

        print(f"[orchestrator_v2] 자동 업로드 → {len(image_urls)}장")

        from routers.instagram import publish_photos
        media_id = publish_photos(insta_user_id, insta_access_token, image_urls, full_caption)

        save_post_data(shop_id, {
            "id":                 post_id,
            "caption":            caption,
            "hashtags":           hashtags,
            "photo_ids":          [p.get("id") for p in selected_photos],
            "cta":                cta,
            "status":             "success",
            "instagram_media_id": media_id
        })
        return True

    except Exception as e:
        print(f"[orchestrator_v2] 자동 업로드 실패: {e}")
        return False


async def _send_push_notification(shop_id, post_id, post_draft):
    try:
        from services.cosmos_db import get_auth
        shop_auth   = get_auth(shop_id) or {}
        owner_email = shop_auth.get("owner_email") or shop_auth.get("gmail")
        if not owner_email: return
        from services.email_service import send_draft_notification
        await send_draft_notification(owner_email, post_id, post_draft.get("caption", ""))
    except Exception as e:
        print(f"[orchestrator_v2] 알림 에러 (무시): {e}")