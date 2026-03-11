import os
import json
import asyncio
import time
from datetime import datetime, timezone, timedelta
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from agents.web_search import web_search_agent
from agents.photo_select import photo_select_agent
from agents.post_writer import post_writer_agent
from agents.rag_tool import search_rag_context

KST = timezone(timedelta(hours=9))

QUALITY_THRESHOLD = 0.7
MAX_RETRY = 2
MIN_PHOTO_COUNT = 1

MODEL_TIER = {
    "mini": "mini",
    "full": "full"
}


def _get_deployment_name(tier: str) -> str:
    """
    우선순위:
    1. AZURE_OPENAI_DEPLOYMENT_MINI / FULL
    2. AZURE_OPENAI_DEPLOYMENT (fallback)
    """
    if tier == "mini":
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    else:
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or os.getenv("AZURE_OPENAI_DEPLOYMENT")

    if not deployment:
        raise ValueError(
            f"Azure OpenAI 배포 이름이 설정되지 않았습니다!\n"
            f".env 파일에 AZURE_OPENAI_DEPLOYMENT_MINI 또는 AZURE_OPENAI_DEPLOYMENT 를 추가하세요."
        )
    return deployment


def _classify_complexity(trigger: str, photo_ids: list = None) -> str:
    """
    요청 복잡도를 분류해서 사용할 모델 티어 반환

    auto + 사진 5장 미만  -> mini
    manual OR 사진 5장+  -> full
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

    print(f"[orchestrator] STEP 0 복잡도 분류 -> {tier} ({reason})")
    return tier

# 메인 파이프라인
async def run_pipeline(
    shop_id: str,
    trigger: str,
    photo_ids: list = None,
) -> dict:
    """
    에이전트 오케스트레이터 메인 함수.
    agent.py 라우터에서 호출.

    AG-050: 전체 및 스텝별 elapsed_seconds 측정 -> draft 저장 + 반환값 포함
    """
    # AG-050: 파이프라인 타이머 시작
    pipeline_start = time.time()
    step_timings: dict = {}
    now_kst = datetime.now(KST)
    print(f"\n{'='*60}")
    print(f"[orchestrator] 파이프라인 시작 -> {now_kst.strftime('%Y-%m-%d %H:%M:%S KST')}")
    print(f"  shop_id : {shop_id}  |  trigger : {trigger}")
    print(f"{'='*60}")

    # STEP 0: 복잡도 분류
    tier = _classify_complexity(trigger, photo_ids)
    kernel = _init_kernel(tier)
    total_retries = 0

    # STEP 1: 병렬 데이터 수집
    print(f"[orchestrator] STEP 1 병렬 수집 시작")
    _t = time.time()

    trend_data, brand_settings, photo_candidates, recent_posts = await asyncio.gather(
        web_search_agent(shop_id),
        _get_brand_settings(shop_id),
        _get_photo_candidates(shop_id),
        _get_recent_posts(shop_id)
    )

    step_timings["step1_data_collection"] = round(time.time() - _t, 2)
    print(f"[orchestrator] STEP 1 완료 -> {step_timings['step1_data_collection']}s")

    # STEP 2: 트렌드 품질 평가
    _t = time.time()
    trend_score = await _evaluate_trend(kernel, trend_data)
    print(f"[orchestrator] STEP 2 트렌드 품질 점수: {trend_score:.2f}")

    retry_count = 0
    while trend_score < QUALITY_THRESHOLD and retry_count < MAX_RETRY:
        retry_count += 1
        total_retries += 1
        print(f"[orchestrator] 트렌드 품질 미달 -> 재시도 {retry_count}/{MAX_RETRY}")
        trend_data = await web_search_agent(shop_id, force_refresh=True)
        trend_score = await _evaluate_trend(kernel, trend_data)
        print(f"[orchestrator] 재시도 후 트렌드 점수: {trend_score:.2f}")

        if retry_count >= MAX_RETRY and trend_score < QUALITY_THRESHOLD and tier == "mini":
            print(f"[orchestrator] 재시도 소진 -> GPT-4.1로 승격")
            tier = "full"
            kernel = _init_kernel(tier)

    step_timings["step2_trend_eval"] = round(time.time() - _t, 2)
    print(f"[orchestrator] STEP 2 완료 -> {step_timings['step2_trend_eval']}s")

    # STEP 3: 사진 선택
    _t = time.time()

    if trigger == "auto":
        selected_photos = await photo_select_agent(
            shop_id=shop_id,
            trend_data=trend_data,
            photo_candidates=photo_candidates,
            brand_settings=brand_settings
        )
        if len(selected_photos) < MIN_PHOTO_COUNT:
            print(f"[orchestrator] 사진 부족 ({len(selected_photos)}장) -> 날짜 범위 확장")
            extended_candidates = await _get_photo_candidates(shop_id, extend_days=30)
            selected_photos = await photo_select_agent(
                shop_id=shop_id,
                trend_data=trend_data,
                photo_candidates=extended_candidates,
                brand_settings=brand_settings
            )
            print(f"[orchestrator] 확장 후 사진 수: {len(selected_photos)}장")
    else:
        if photo_ids is None or len(photo_ids) == 0:
            print(f"[orchestrator] manual이지만 photo_ids 없음 -> 자동 선택 모드")
            selected_photos = await photo_select_agent(
                shop_id=shop_id,
                trend_data=trend_data,
                photo_candidates=photo_candidates,
                brand_settings=brand_settings
            )
        else:
            print(f"[orchestrator] manual -> 사장님 선택 사진 {len(photo_ids)}장 사용")
            selected_photos = await _get_photos_by_ids(shop_id, photo_ids)

    step_timings["step3_photo_select"] = round(time.time() - _t, 2)
    print(f"[orchestrator] STEP 3 완료 -> {step_timings['step3_photo_select']}s  |  선택 사진: {len(selected_photos)}장")

    # STEP 4: RAG
    _t = time.time()
    print(f"[orchestrator] STEP 4 RAG 시작")

    rag_context = await search_rag_context(
        shop_id=shop_id,
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts
    )

    step_timings["step4_rag"] = round(time.time() - _t, 2)
    print(f"[orchestrator] STEP 4 RAG 완료 -> {step_timings['step4_rag']}s")

    # STEP 5: 게시물 작성 + Self-Eval
    _t = time.time()

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
        print(f"[orchestrator] 캡션 품질 미달 -> 재작성 지시 {retry_count}/{MAX_RETRY}")

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

        if retry_count >= MAX_RETRY and caption_score < QUALITY_THRESHOLD and tier == "mini":
            print(f"[orchestrator] 재시도 소진 -> GPT-4.1로 승격해서 최종 재작성")
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
            break

    step_timings["step5_post_writer"] = round(time.time() - _t, 2)
    print(f"[orchestrator] STEP 5 완료 -> {step_timings['step5_post_writer']}s")

    # STEP 6: 초안 저장 + 알림 (AG-050: elapsed 포함)
    _t = time.time()
    elapsed_seconds = round(time.time() - pipeline_start, 2)

    post_id = await _generate_post_id()
    await asyncio.gather(
        _save_draft(
            shop_id, post_id, post_draft, selected_photos,
            elapsed_seconds=elapsed_seconds,
            step_timings=step_timings,
            model_used=tier,
            caption_score=round(caption_score, 2),
            trend_score=round(trend_score, 2),
        ),
        _send_push_notification(shop_id, post_draft)
    )

    step_timings["step6_save_draft"] = round(time.time() - _t, 2)

    # AG-050: 최종 로깅
    total_elapsed = round(time.time() - pipeline_start, 2)
    print(f"\n{'='*60}")
    print(f"[orchestrator] 파이프라인 완료")
    print(f"  post_id      : {post_id}")
    print(f"  총 실행 시간 : {total_elapsed}s  (태경님 스케줄러 -40분 타이밍 참고용)")
    print(f"  모델         : {tier}  |  총 재시도: {total_retries}회")
    print(f"  캡션 점수    : {caption_score:.2f}  |  트렌드 점수: {trend_score:.2f}")
    print(f"  스텝별 시간  : {step_timings}")
    print(f"{'='*60}\n")

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
            "model_used": tier,
            "elapsed_seconds": total_elapsed,       # AG-050
            "step_timings": step_timings,            # AG-050
        }
    }

# 평가 함수
async def _evaluate_trend(kernel: Kernel, trend_data: dict) -> float:
    """트렌드 결과 품질 평가 (0.0 ~ 1.0)"""
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
    """캡션 품질 다면 평가 5개 차원 (가중 평균, 예약 문의 40%)"""
    try:
        caption = post_draft.get("caption", "")
        hashtags = post_draft.get("hashtags", [])
        cta = post_draft.get("cta", "")

        brand_tone = brand_settings.get("brand_tone", "")
        if isinstance(brand_tone, list):
            brand_tone = " ".join(brand_tone)

        forbidden_words = brand_settings.get("forbidden_words", [])
        if isinstance(forbidden_words, str):
            forbidden_words = [w.strip() for w in forbidden_words.split(",")]

        if not caption:
            return 0.0

        chat_history = ChatHistory()
        chat_history.add_user_message(
            f"""너는 바버샵 마케팅 전문가야.
아래 인스타그램 캡션을 5개 차원으로 평가해줘.

[캡션]
{caption}

[해시태그]
{' '.join(hashtags)}

[CTA]
{cta}

[브랜드 설정]
- 톤: {brand_tone}
- 금칙어: {', '.join(forbidden_words) if forbidden_words else '없음'}

[평가 기준 5가지] (각 0.0~1.0)

1. reservation_inquiry (예약 문의 전환율)
   - CTA 긴박감: "지금 DM 주시면" (0.8) vs "문의주세요" (0.4)
   - 행동 유도: "오늘 3자리 남음" (0.9)

2. fade_keyword (페이드컷 키워드)
   - 첫 문장에 "페이드" 포함? -> 1.0 / 본문 포함 -> 0.7 / 미포함 -> 0.3

3. cta_strength (CTA 강도)
   - 수동적 (0.3) / 능동적 (0.7) / 초강력 (1.0)

4. brand_tone (브랜드 톤)
   - 설정된 톤과 일치? 금칙어 포함 시 -0.5

5. target_appeal (타겟 어필)
   - 20-40대 남성이 공감하는 문구?

JSON으로만 응답:
{{
  "reservation_inquiry": 0.0,
  "fade_keyword": 0.0,
  "cta_strength": 0.0,
  "brand_tone": 0.0,
  "target_appeal": 0.0
}}"""
        )

        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        raw = str(response).strip().replace("```json", "").replace("```", "").strip()
        scores = json.loads(raw)

        total_score = (
            scores.get("reservation_inquiry", 0.5) * 0.40 +
            scores.get("fade_keyword", 0.5) * 0.25 +
            scores.get("cta_strength", 0.5) * 0.15 +
            scores.get("brand_tone", 0.5) * 0.10 +
            scores.get("target_appeal", 0.5) * 0.10
        )

        print(f"[orchestrator] 캡션 평가 상세:")
        print(f"  - 예약문의율 : {scores.get('reservation_inquiry', 0):.2f}")
        print(f"  - 페이드키워드: {scores.get('fade_keyword', 0):.2f}")
        print(f"  - CTA강도    : {scores.get('cta_strength', 0):.2f}")
        print(f"  - 브랜드톤   : {scores.get('brand_tone', 0):.2f}")
        print(f"  - 타겟어필   : {scores.get('target_appeal', 0):.2f}")

        return max(0.0, min(1.0, total_score))

    except Exception as e:
        print(f"[orchestrator] 캡션 평가 실패: {e}")
        return 0.5

# 유틸
def _init_kernel(tier: str = "mini") -> Kernel:
    deployment = _get_deployment_name(tier)
    print(f"[orchestrator] Kernel 초기화 -> tier={tier}, deployment={deployment}")
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel

# DB 연동 함수 (fallback 데이터 포함)
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

    def to_list(val):
        if isinstance(val, list):
            return val
        if isinstance(val, str) and val:
            return [v.strip() for v in val.split(",")]
        return []

    shop = data.get("shop_info", {})
    return {
        "brand_tone": shop.get("brand_tone", "친근하고 편안한 말투"),
        "forbidden_words": to_list(shop.get("forbidden_words")),
        "preferred_styles": to_list(shop.get("preferred_styles")),
        "exclude_conditions": to_list(shop.get("exclude_conditions")),
        "hashtag_style": shop.get("hashtag_style", "감성형"),
        "cta": shop.get("cta", "DM으로 예약 문의주세요"),
        "photo_range": {"min": 1, "max": 5},
        "feed_style": shop.get("feed_style", {}),
        "brand_differentiation": shop.get("shop_intro", "")
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


async def _save_draft(
    shop_id: str,
    post_id: str,
    post_draft: dict,
    selected_photos: list,
    elapsed_seconds: float = 0.0,
    step_timings: dict = None,
    model_used: str = "mini",
    caption_score: float = 0.0,
    trend_score: float = 0.0,
):
    """
    초안 저장.
    AG-050: elapsed_seconds / step_timings / model_used / caption_score 포함.
    태경님 스케줄러가 -40분 타이밍 설정 시 elapsed_seconds를 실측 근거로 사용.
    """
    from services.cosmos_db import save_draft
    now_kst = datetime.now(KST)
    review_deadline = now_kst + timedelta(minutes=29)

    save_draft(
        shop_id=shop_id,
        post_id=post_id,
        caption=post_draft.get("caption", ""),
        hashtags=post_draft.get("hashtags", []),
        photo_ids=[p.get("id", p.get("photo_id")) for p in selected_photos],
        cta=post_draft.get("cta", ""),
        # AG-050
        elapsed_seconds=elapsed_seconds,
        step_timings=step_timings or {},
        model_used=model_used,
        caption_score=caption_score,
        trend_score=trend_score,
        review_deadline=review_deadline.isoformat(),
        created_at=now_kst.isoformat(),
    )


async def _send_push_notification(shop_id: str, post_draft: dict):
    # TODO: Gmail 알림 (태경님 구현 후 연결)
    pass


async def _generate_post_id() -> str:
    import uuid
    return f"post_{uuid.uuid4().hex[:8]}"