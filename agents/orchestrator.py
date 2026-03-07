import os
import asyncio
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from agents.web_search import web_search_agent
from agents.photo_select import photo_select_agent
from agents.post_writer import post_writer_agent
from agents.rag_tool import search_rag_context

MODEL_TIER = {
    "mini": os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI", os.getenv("AZURE_OPENAI_DEPLOYMENT")),
    "full": os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL", os.getenv("AZURE_OPENAI_DEPLOYMENT")),
}


# [설정] 품질 게이팅 임계값
QUALITY_THRESHOLD = 0.7     # 이 점수 미만이면 재시도
MAX_RETRY = 2               # 초과 시 GPT-4.1로 승격
MIN_PHOTO_COUNT = 1         # 최소 사진 수 (부족 시 날짜 범위 확장)


# [STEP 0] 
def _classify_complexity(trigger: str, photo_ids: list = None) -> str:
    """
    요청 복잡도를 분류해서 사용할 모델 티어 반환

    단순 → "mini" (GPT-4.1-mini)
    복잡 → "full" (GPT-4.1)

    복잡도 판단 기준:
    - trigger == 'manual': 사장님이 직접 개입 → 더 신중한 처리 필요
    - photo_ids >= 5장: 대량 사진 분석 → 더 강한 추론 필요
    """
    if trigger == "manual":
        reason = "manual 트리거 (사장님 직접 개입)"
        tier = "full"
    elif photo_ids and len(photo_ids) >= 5:
        reason = f"사진 {len(photo_ids)}장 대량 처리"
        tier = "full"
    else:
        reason = "일반 자동 실행"
        tier = "mini"

    print(f"[orchestrator] STEP 0 복잡도 분류 → {tier} ({reason})")
    print(f"[orchestrator] 사용 모델: {MODEL_TIER[tier]}")
    return tier

# [메인] 오케스트레이터 진입점
async def run_pipeline(
    shop_id: str,
    trigger: str,           # 'auto' | 'manual'
    photo_ids: list = None  # 수동일 때만
) -> dict:
    """
    에이전트 오케스트레이터 메인 함수

    Args:
        shop_id:   샵 고유 ID
        trigger:   'auto' (자동 예약) | 'manual' (사장님 수동 실행)
        photo_ids: 수동 모드일 때 사장님이 선택한 사진 ID 리스트

    Returns:
        {
            "post_id": "post_abc12345",
            "caption": "...",
            "hashtags": [...],
            "photo_urls": [...],
            "cta": "DM으로 예약 문의주세요",
            "status": "draft",
            "quality": {
                "trend_score": 0.85,
                "caption_score": 0.90,
                "retries": 0,
                "model_used": "mini"    ← 실제로 어떤 모델 사용했는지 기록
            }
        }
    """
    # STEP 0
    tier = _classify_complexity(trigger, photo_ids)
    kernel = _init_kernel(tier)
    total_retries = 0

    # STEP 1
    print(f"[orchestrator] STEP 1 시작 → shop_id={shop_id}, trigger={trigger}")

    trend_data, brand_settings, photo_candidates, recent_posts = await asyncio.gather(
        web_search_agent(shop_id),
        _get_brand_settings(shop_id),
        _get_photo_candidates(shop_id),
        _get_recent_posts(shop_id)
    )

    # STEP 2
    trend_score = await _evaluate_trend(kernel, trend_data)
    print(f"[orchestrator] STEP 2 트렌드 품질 점수: {trend_score:.2f}")

    retry_count = 0
    while trend_score < QUALITY_THRESHOLD and retry_count < MAX_RETRY:
        retry_count += 1
        total_retries += 1
        print(f"[orchestrator] 트렌드 품질 미달 → 재시도 {retry_count}/{MAX_RETRY}")
        trend_data = await web_search_agent(shop_id, force_refresh=True)
        trend_score = await _evaluate_trend(kernel, trend_data)
        print(f"[orchestrator] 재시도 후 트렌드 점수: {trend_score:.2f}")

        if retry_count >= MAX_RETRY and trend_score < QUALITY_THRESHOLD and tier == "mini":
            print(f"[orchestrator] 재시도 소진 → GPT-4.1로 승격해서 마지막 평가")
            tier = "full"
            kernel = _init_kernel(tier)

    # STEP 3
    if trigger == "auto":
        selected_photos = await photo_select_agent(
            shop_id=shop_id,
            trend_data=trend_data,
            photo_candidates=photo_candidates,
            brand_settings=brand_settings
        )

        if len(selected_photos) < MIN_PHOTO_COUNT:
            print(f"[orchestrator] 사진 부족 ({len(selected_photos)}장) → 날짜 범위 확장 후 재요청")
            extended_candidates = await _get_photo_candidates(shop_id, extend_days=30)
            selected_photos = await photo_select_agent(
                shop_id=shop_id,
                trend_data=trend_data,
                photo_candidates=extended_candidates,
                brand_settings=brand_settings
            )
            print(f"[orchestrator] 확장 후 사진 수: {len(selected_photos)}장")

    elif trigger == "manual":
        selected_photos = await _get_photos_by_ids(shop_id, photo_ids)

    print(f"[orchestrator] STEP 3 완료 → 선택된 사진: {len(selected_photos)}장")

    # STEP 4
    print(f"[orchestrator] STEP 4 RAG 시작")

    rag_context = await search_rag_context(
        shop_id=shop_id,
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts
    )

    post_draft = await post_writer_agent(
        shop_id=shop_id,
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts,
        rag_context=rag_context
    )

    caption_score = await _evaluate_caption(kernel, post_draft, brand_settings)
    print(f"[orchestrator] STEP 5 캡션 품질 점수: {caption_score:.2f}")

    retry_count = 0
    while caption_score < QUALITY_THRESHOLD and retry_count < MAX_RETRY:
        retry_count += 1
        total_retries += 1
        print(f"[orchestrator] 캡션 품질 미달 → 재작성 지시 {retry_count}/{MAX_RETRY}")

        post_draft = await post_writer_agent(
            shop_id=shop_id,
            trend_data=trend_data,
            selected_photos=selected_photos,
            brand_settings=brand_settings,
            recent_posts=recent_posts,
            rag_context=rag_context,
            previous_draft=post_draft,
            feedback=f"브랜드 톤 점수 {caption_score:.2f} 미달. 금칙어 제거 및 톤 재조정 필요."
        )
        caption_score = await _evaluate_caption(kernel, post_draft, brand_settings)
        print(f"[orchestrator] 재작성 후 캡션 점수: {caption_score:.2f}")

        # 재시도 2회 소진 + 여전히 미달 → GPT-4.1로 승격 후 최종 시도
        if retry_count >= MAX_RETRY and caption_score < QUALITY_THRESHOLD and tier == "mini":
            print(f"[orchestrator] 재시도 소진 → GPT-4.1로 승격해서 최종 재작성")
            tier = "full"
            kernel = _init_kernel(tier)
            post_draft = await post_writer_agent(
                shop_id=shop_id,
                trend_data=trend_data,
                selected_photos=selected_photos,
                brand_settings=brand_settings,
                recent_posts=recent_posts,
                rag_context=rag_context,
                previous_draft=post_draft,
                feedback="최고 품질로 재작성 필요. 브랜드 톤 완벽 준수."
            )
            caption_score = await _evaluate_caption(kernel, post_draft, brand_settings)
            print(f"[orchestrator] GPT-4.1 승격 후 캡션 점수: {caption_score:.2f}")
            break   # 승격 후에는 더 이상 루프 없이 결과 사용

    # STEP 6
    post_id = await _generate_post_id()
    await asyncio.gather(
        _save_draft(shop_id, post_id, post_draft, selected_photos),
        _send_push_notification(shop_id, post_draft)
    )

    print(f"[orchestrator] 완료 → post_id={post_id}, 모델={tier}, 총 재시도={total_retries}회")

    return {
        "post_id": post_id,
        "caption": post_draft["caption"],
        "hashtags": post_draft["hashtags"],
        "photo_urls": [p["blob_url"] for p in selected_photos],
        "cta": post_draft["cta"],
        "status": "draft",
        "quality": {
            "trend_score": round(trend_score, 2),
            "caption_score": round(caption_score, 2),
            "retries": total_retries,
            "model_used": tier      # 실제로 어떤 모델 사용했는지
        }
    }


# [평가]
async def _evaluate_trend(kernel: Kernel, trend_data: dict) -> float:
    """
    트렌드 결과 품질 평가
    - 바버샵 전용 스타일 키워드 포함 여부
    - 여성 헤어, 미용실 관련 내용 혼입 여부
    - 0.0 (완전 무관) ~ 1.0 (완벽) 점수 반환
    """
    try:
        trend_text = trend_data.get("trend", "")
        if not trend_text:
            return 0.0

        chat_history = ChatHistory()
        chat_history.add_user_message(
            "아래 트렌드 요약이 바버샵 남성 헤어스타일 전용 내용인지 평가해줘.\n"
            "평가 기준:\n"
            "- 페이드컷, 사이드파트, 슬릭백 등 바버샵 스타일 포함 여부\n"
            "- 여성 헤어, 펌, 염색, 미용실 내용이 없어야 함\n"
            "- 구체적인 스타일명이 2개 이상 포함되어야 함\n\n"
            "0.0에서 1.0 사이의 숫자만 출력해. 다른 말은 하지 마.\n\n"
            f"트렌드 요약:\n{trend_text}"
        )

        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        score = float(str(response).strip())
        return max(0.0, min(1.0, score))

    except Exception as e:
        print(f"[orchestrator] 트렌드 평가 실패: {e}")
        return 0.5


async def _evaluate_caption(kernel: Kernel, post_draft: dict, brand_settings: dict) -> float:
    """
    캡션 품질 평가
    - 브랜드 톤 준수 여부
    - 금칙어 포함 여부
    - 해시태그 적절성
    - 0.0 ~ 1.0 점수 반환
    """
    try:
        caption = post_draft.get("caption", "")
        hashtags = post_draft.get("hashtags", [])
        brand_tone = brand_settings.get("brand_tone", "")
        forbidden_words = brand_settings.get("forbidden_words", [])

        if not caption:
            return 0.0

        chat_history = ChatHistory()
        chat_history.add_user_message(
            "아래 인스타그램 캡션이 브랜드 기준에 맞는지 평가해줘.\n\n"
            f"브랜드 톤: {brand_tone}\n"
            f"금칙어 (포함되면 안 됨): {', '.join(forbidden_words)}\n\n"
            "평가 기준:\n"
            "- 브랜드 톤과 말투가 일치하는가\n"
            "- 금칙어가 포함되지 않았는가\n"
            "- 자연스럽고 매력적인 문장인가\n"
            "- 바버샵 관련 내용인가\n\n"
            "0.0에서 1.0 사이의 숫자만 출력해. 다른 말은 하지 마.\n\n"
            f"캡션:\n{caption}\n\n"
            f"해시태그: {' '.join(hashtags)}"
        )

        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        score = float(str(response).strip())
        return max(0.0, min(1.0, score))

    except Exception as e:
        print(f"[orchestrator] 캡션 평가 실패: {e}")
        return 0.5

def _init_kernel(tier: str = "mini") -> Kernel:
    """
    티어에 따라 다른 모델로 Kernel 초기화

    Args:
        tier: "mini" → GPT-4.1-mini (기본)
              "full" → GPT-4.1 (복잡한 요청 / 재시도 소진 시 승격)

    Note:
        GPT-4.1 발급 전까지는 DEPLOYMENT_FULL이 없어서
        자동으로 현재 배포(AZURE_OPENAI_DEPLOYMENT)로 fallback됨
    """
    deployment = MODEL_TIER.get(tier, MODEL_TIER["mini"])
    print(f"[orchestrator] Kernel 초기화 → tier={tier}, deployment={deployment}")

    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel

# TODO: 아래 함수들은 지연님 함수로 교체

async def _get_brand_settings(shop_id: str) -> dict:
    from services.cosmos_db import get_onboarding
    data = get_onboarding(shop_id)
    if not data:
        return {
            "brand_tone": "친근하고 편안한 말투",
            "forbidden_words": ["저렴", "할인"],
            "cta": "DM으로 예약 문의주세요",
            "photo_range": {"min": 1, "max": 5}
        }
    survey = data.get("survey_answers", {})
    return {
        "brand_tone": survey.get("brand_tone", "친근하고 편안한 말투"),
        "forbidden_words": survey.get("forbidden_words", []),
        "cta": survey.get("cta", "DM으로 예약 문의주세요"),
        "photo_range": survey.get("photo_range", {"min": 1, "max": 5}),
        "feed_style": survey.get("feed_style", {})
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


async def _save_draft(shop_id: str, post_id: str, post_draft: dict, selected_photos: list):
    from services.cosmos_db import save_draft
    save_draft(
        shop_id=shop_id,
        post_id=post_id,
        caption=post_draft.get("caption", ""),
        hashtags=post_draft.get("hashtags", []),
        photo_ids=[p.get("id", p.get("photo_id")) for p in selected_photos],
        cta=post_draft.get("cta", "")
    )


async def _send_push_notification(shop_id: str, post_draft: dict):
    # TODO: Windows 토스트 알림 + 이메일 백업
    pass


async def _generate_post_id() -> str:
    import uuid
    return f"post_{uuid.uuid4().hex[:8]}"