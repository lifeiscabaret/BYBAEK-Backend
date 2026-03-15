import os
import json
import asyncio
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from agents.web_search import web_search_agent
from agents.photo_select import photo_select_agent
from agents.post_writer import post_writer_agent
from agents.rag_tool import search_rag_context

# [환경변수 검증]
def _get_deployment_name(tier: str) -> str:
    if tier == "mini":
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    else:  # tier == "full"
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    
    if not deployment:
        raise ValueError(
            f"❌ Azure OpenAI 배포 이름이 설정되지 않았습니다!\n\n"
            f".env 파일에 다음 중 하나를 추가하세요:\n"
            f"  AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini-BYBAEK\n"
            f"또는\n"
            f"  AZURE_OPENAI_DEPLOYMENT_MINI=gpt-4.1-mini-BYBAEK\n"
            f"  AZURE_OPENAI_DEPLOYMENT_FULL=gpt-4.1-BYBAEK\n"
        )
    
    return deployment

MODEL_TIER = {
    "mini": "mini",  # _get_deployment_name()으로 실제 이름 조회
    "full": "full"
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
        # ✅ 수정: photo_ids가 None이면 자동 선택
        if photo_ids is None or len(photo_ids) == 0:
            print(f"[orchestrator] manual이지만 photo_ids 없음 → 자동 선택 모드")
            selected_photos = await photo_select_agent(
                shop_id=shop_id,
                trend_data=trend_data,
                photo_candidates=photo_candidates,
                brand_settings=brand_settings
            )
        else:
            print(f"[orchestrator] manual → 사장님 선택 사진 {len(photo_ids)}장 사용")
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
    _val = brand_settings.get("insta_review_bfr_upload_yn", "Y")
    need_review = str(_val).upper() != "N"

    await _save_draft(shop_id, post_id, post_draft, selected_photos)

    if not need_review:
        # 자동 업로드: 검토 없이 바로 인스타 업로드
        print(f"[orchestrator] STEP 6 자동 업로드 (insta_review_bfr_upload_yn=False)")
        upload_status = await _auto_upload_instagram(shop_id, post_id, post_draft, selected_photos)
        status_str = "uploaded" if upload_status else "draft"
    else:
        # 검토 후 업로드: 푸시 알림만 발송
        print(f"[orchestrator] STEP 6 검토 대기 (insta_review_bfr_upload_yn=True)")
        await _send_push_notification(shop_id, post_id, post_draft)
        status_str = "draft"

    print(f"[orchestrator] 완료 → post_id={post_id}, 모델={tier}, 총 재시도={total_retries}회, 상태={status_str}")

    return {
        "post_id": post_id,
        "caption": post_draft["caption"],
        "hashtags": post_draft["hashtags"],
        "photo_urls": [p["blob_url"] for p in selected_photos],
        "cta": post_draft["cta"],
        "status": status_str,
        "quality": {
            "trend_score": round(trend_score, 2),
            "caption_score": round(caption_score, 2),
            "retries": total_retries,
            "model_used": tier
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
    캡션 품질 다면 평가 (5개 차원)
    
    바버샵 특화 평가:
    - reservation_inquiry: 예약 문의 전환율 예측
    - fade_keyword: 페이드컷 키워드 배치 (고객 1순위 니즈)
    - cta_strength: CTA 강도
    - brand_tone: 브랜드 톤
    - target_appeal: 타겟 어필
    
    Returns:
        float: 0.0~1.0 (가중 평균)
    """
    try:
        caption = post_draft.get("caption", "")
        hashtags = post_draft.get("hashtags", [])
        cta = post_draft.get("cta", "")
        
        # ✅ brand_tone 리스트 처리
        brand_tone = brand_settings.get("brand_tone", "")
        if isinstance(brand_tone, list):
            brand_tone = " ".join(brand_tone)
        
        # ✅ forbidden_words 리스트 처리
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
   - 이 캡션을 보고 예약 DM 보낼 확률은?
   - CTA 긴박감: "지금 DM 주시면" (0.8) vs "문의주세요" (0.4)
   - 사실 기반 행동 유도 문구 포함 여부 (0.7 이상)

2. fade_keyword (페이드컷 키워드)
   - 고객 1순위 니즈: 페이드컷
   - 첫 문장에 "페이드" 포함? → 1.0
   - 본문 어디든 포함? → 0.7
   - 미포함? → 0.3

3. cta_strength (CTA 강도)
   - 수동적 (0.3): "문의주세요"
   - 능동적 (0.7): "지금 DM 주시면"
   - 강력 (1.0): 즉각 행동 유도 + 구체적 혜택

4. brand_tone (브랜드 톤)
   - 설정된 톤과 일치?
   - 금칙어 포함 시 -0.5

5. target_appeal (타겟 어필)
   - 20-40대 남성이 공감하는 문구?
   - 직장인: 깔끔함, 전문성
   - 대학생: 트렌디함, 스타일

0.0~1.0 숫자 5개만 출력 (JSON):
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

        raw = str(response).strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        scores = json.loads(raw)

        # 가중 평균 (예약 문의 40%)
        total_score = (
            scores.get("reservation_inquiry", 0.5) * 0.40 +
            scores.get("fade_keyword", 0.5) * 0.25 +
            scores.get("cta_strength", 0.5) * 0.15 +
            scores.get("brand_tone", 0.5) * 0.10 +
            scores.get("target_appeal", 0.5) * 0.10
        )
        
        print(f"[orchestrator] 캡션 평가 상세:")
        print(f"  - 예약문의율: {scores.get('reservation_inquiry', 0):.2f}")
        print(f"  - 페이드키워드: {scores.get('fade_keyword', 0):.2f}")
        print(f"  - CTA강도: {scores.get('cta_strength', 0):.2f}")

        return max(0.0, min(1.0, total_score))

    except Exception as e:
        print(f"[orchestrator] 캡션 평가 실패: {e}")
        return 0.5

def _init_kernel(tier: str = "mini") -> Kernel:
    """
    티어에 따라 다른 모델로 Kernel 초기화

    Args:
        tier: "mini" → GPT-4.1-mini (기본)
              "full" → GPT-4.1 (복잡한 요청 / 재시도 소진 시 승격)

    Raises:
        ValueError: 환경변수가 없을 때
    """
    deployment = _get_deployment_name(tier)
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

    def to_list(val):
        if isinstance(val, list): return val
        if isinstance(val, str) and val: return [v.strip() for v in val.split(",")]
        return []

    shop = data.get("shop_info", {})
    return {
        "brand_tone": shop.get("brand_tone", "친근하고 편안한 말투"),
        "forbidden_words": to_list(shop.get("forbidden_words")),
        "preferred_styles": to_list(shop.get("preferred_styles")),
        "cta": shop.get("cta", "DM으로 예약 문의주세요"),
        "photo_range": {"min": 1, "max": 5},
        "feed_style": shop.get("feed_style", {}),
        "brand_differentiation": shop.get("shop_intro", ""),
        "insta_review_bfr_upload_yn": str(shop.get("insta_review_bfr_upload_yn", "Y")).upper() != "N"  # DB값 "Y"/"N" 문자열 대응
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
        cta=post_draft.get("cta", ""),
        review_action="pending"
    )


async def _auto_upload_instagram(
    shop_id: str,
    post_id: str,
    post_draft: dict,
    selected_photos: list
) -> bool:
    """
    검토 없이 자동 업로드.
    insta_review_bfr_upload_yn=False 일 때 STEP 6에서 호출.
    """
    try:
        from services.cosmos_db import get_auth, save_post_data

        # 1. 인스타 인증 정보 조회
        shop_auth = get_auth(shop_id)
        if not shop_auth:
            print(f"[orchestrator] 인스타 인증 정보 없음 → 업로드 스킵")
            return False

        insta_user_id    = shop_auth.get("insta_user_id")
        insta_access_token = shop_auth.get("insta_access_token")

        if not insta_user_id or not insta_access_token:
            print(f"[orchestrator] insta_user_id 또는 access_token 없음 → 업로드 스킵")
            return False

        # 2. 캡션 구성 (caption + hashtags + cta)
        caption    = post_draft.get("caption", "")
        hashtags   = post_draft.get("hashtags", [])
        cta        = post_draft.get("cta", "")
        full_caption = (caption + "\n\n" + " ".join(hashtags) + "\n" + cta).strip()

        # 3. 이미지 URL 리스트
        image_urls = [p["blob_url"] for p in selected_photos if p.get("blob_url")]
        if not image_urls:
            print(f"[orchestrator] 업로드할 이미지 없음 → 스킵")
            return False

        # 4. Instagram 업로드 호출
        from routers.instagram import create_image_container, create_carousel_container, publish_container
        container_ids = [
            create_image_container(insta_user_id, insta_access_token, url)
            for url in image_urls
        ]
        creation_id = create_carousel_container(insta_user_id, insta_access_token, container_ids, full_caption)
        media_id    = publish_container(insta_user_id, creation_id, insta_access_token)

        # 5. 업로드 결과 저장
        save_post_data(shop_id, {
            "id":           post_id,
            "caption":      caption,
            "hashtags":     hashtags,
            "photo_ids":    [p.get("id", p.get("photo_id")) for p in selected_photos],
            "cta":          cta,
            "status":       "success",
            "instagram_media_id": media_id
        })

        print(f"[orchestrator] 자동 업로드 성공 → media_id={media_id}")
        return True

    except Exception as e:
        print(f"[orchestrator] 자동 업로드 실패: {e} → draft 상태 유지")
        return False


async def _send_push_notification(shop_id: str, post_id: str, post_draft: dict):
    """
    초안 완성 알림: Gmail 발송.
    shop_id로 사장님 이메일 조회 후 send_draft_notification() 호출.
    """
    try:
        from services.cosmos_db import get_auth
        shop_auth   = get_auth(shop_id) or {}
        owner_email = shop_auth.get("owner_email") or shop_auth.get("gmail")

        if not owner_email:
            print(f"[orchestrator] 이메일 없음 → 알림 스킵 (shop_id={shop_id})")
            return

        from services.email_service import send_draft_notification
        caption = post_draft.get("caption", "")
        success = await send_draft_notification(owner_email, post_id, caption)

        if success:
            print(f"[orchestrator] 알림 메일 발송 완료 → {owner_email}")
        else:
            print(f"[orchestrator] 알림 메일 발송 실패 → {owner_email}")

    except Exception as e:
        print(f"[orchestrator] 푸시 알림 에러 (무시): {e}")


async def _generate_post_id() -> str:
    import uuid
    return f"post_{uuid.uuid4().hex[:8]}"