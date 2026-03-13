import os
import json
import re
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory

# [메인] orchestrator에서 호출
async def post_writer_agent(
    shop_id: str,
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list,
    rag_context: dict,
    previous_draft: dict = None,    # 재작성 시 이전 초안
    feedback: str = None            # 재작성 시 피드백
) -> dict:
    """
    게시물 작성 에이전트 메인 함수

    orchestrator STEP 4에서 호출.
    최초 작성 또는 재작성(previous_draft + feedback 있을 때) 모두 처리.

    Returns:
        {"caption": "...", "hashtags": [...], "cta": "..."}
    """
    is_rewrite = previous_draft is not None
    mode = "재작성" if is_rewrite else "최초 작성"
    print(f"[post_writer] 시작 → shop_id={shop_id}, 모드={mode}")

    kernel = _init_kernel()

    # 프롬프트 구성
    system_prompt, user_prompt = _build_prompt(
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts,
        rag_context=rag_context,
        previous_draft=previous_draft,
        feedback=feedback
    )

    chat_history = ChatHistory()
    chat_history.add_system_message(system_prompt)
    chat_history.add_user_message(user_prompt)

    try:
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        raw = str(response).strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        # 금칙어 검증 + 자동 제거
        result = _validate_and_clean(result, brand_settings)

        print(f"[post_writer] 완료 → 캡션 {len(result.get('caption', ''))}자, "
              f"해시태그 {len(result.get('hashtags', []))}개")
        return result

    except Exception as e:
        print(f"[post_writer] GPT 실패 ({e}) → fallback 캡션 반환")
        return _fallback_draft(brand_settings, trend_data)

# [프롬프트] 통합 프롬프트 구성
def _build_prompt(
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list,
    rag_context: dict,
    previous_draft: dict = None,
    feedback: str = None
) -> tuple:
    """
    시스템 프롬프트 + 유저 프롬프트 구성

    구조:
      시스템: 역할 + 브랜드 설정 + 응답 형식
      유저:   트렌드 + 사진 스타일 + RAG 예시 + 최근 말투 + (재작성 시 피드백)
    """

    # ── 시스템 프롬프트 ──
    # ✅ brand_tone 리스트 처리
    brand_tone = brand_settings.get("brand_tone", "친근하고 편안한 말투")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)
    
    # ✅ forbidden_words 리스트 처리
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]
    forbidden_str = ", ".join(forbidden_words) if forbidden_words else "없음"
    
    feed_style    = brand_settings.get("feed_style", {})
    emoji_usage   = feed_style.get("emoji_usage", "적당히")
    caption_len   = feed_style.get("caption_length", "2~4줄")
    hashtag_count = feed_style.get("hashtag_count", 10)

    # 시스템 프롬프트 구성
    system_prompt = f"""너는 22년 경력 바버샵 원장님의 마케팅 파트너야.

[원장님 상황]
- 시술로 바쁨 → 홍보 편집 시간 없음 (가장 큰 고민)
- 인스타 광고 + 네이버 체험단: 월 20만원 지출 중
- 목표: 예약 문의 폭발 시켜서 매출 올리기

[고객이 바버샵에 원하는 것]
1순위: 페이드컷 (고객 인터뷰 결과)
- 한국인 두상 울퉁불퉁 → 정교한 그라데이션 기술력 증명 필수

[게시물 전략 5단계]

1. 키워드 전략 (검색 노출)
   - 첫 문장에 "페이드컷" 또는 "페이드" 필수
   - 스타일명 포함: 사이드파트, 슬릭백, 투블럭 등
   - 절대 금지: "cut/컷/자르다" (Azure 필터)
   
2. 기술력 증명 (고객 신뢰)
   - 그라데이션의 자연스러움 강조
   - "정교한", "디테일", "경력" 같은 키워드
   
3. 타겟 고객 저격 (20-40대 남성)
   - 직장인: 깔끔함, 전문성, 시간 절약
   - 대학생: 트렌디함, 스타일 변화
   
4. 예약 문의 유도 (최우선!)
   - 수동적 X: "예약 문의주세요"
   - 능동적 O: "지금 DM 주시면 이번 주 예약 가능"
   - 초강력: "오늘 3자리 남음", "주말 예약 마감 임박"
   
5. 브랜드 톤 준수
   - 말투: {brand_tone}
   - 금칙어: {forbidden_str}
   - 이모지: {emoji_usage}
   - 길이: {caption_len}

[출력 형식 - 엄수]
{{
  "caption": "페이드[스타일]로 시작 + 기술력 강조 + 예약 긴박감\\n줄바꿈 최대 3번",
  "hashtags": ["#페이드컷", "#바버샵", ... 총 {hashtag_count}개],
  "cta": "긴박감 있는 예약 유도 (예: 지금 DM 주시면 이번 주 예약 가능)"
}}

JSON만 출력. 설명/인사말 절대 금지."""

    # ── 유저 프롬프트 ──
    parts = []

     # 1. 오늘 트렌드
    trend_summary = trend_data.get("trend", "")
    weather       = trend_data.get("weather", "")
    promo         = trend_data.get("promo", "")

    parts.append(f"[오늘 트렌드]\n{trend_summary}")
    if weather:
        parts.append(f"[날씨/시즌]\n{weather}")
    if promo:
        parts.append(f"[바버샵 홍보 포인트]\n{promo}")

    # ↓ 샵 차별점 강조 (마케터 전략: 상품 색깔 파악)
    brand_diff = brand_settings.get("brand_differentiation", "")
    if brand_diff:
        parts.append(f"[우리 샵 차별점 - 반드시 첫 문장에 반영]\n{brand_diff}\n\n마케터 팁: 차별점을 메인 키워드와 조합하세요.\n예: '10년 경력 전문가의 페이드 스타일' (차별점 + 키워드)")

    # 2. 선택된 사진 스타일
    if selected_photos:
        style_info = []
        for photo in selected_photos:
            tags = photo.get("style_tags", photo.get("stage2_tags", []))
            if tags:
                style_info.append(f"- {', '.join(tags)}")
        if style_info:
            parts.append(f"[오늘 올릴 사진 스타일]\n" + "\n".join(style_info))

    # 3. RAG 예시 (과거 게시물 패턴)
    if rag_context:
        tone_rules       = rag_context.get("tone_rules", "")
        examples         = rag_context.get("examples", [])
        hashtag_patterns = rag_context.get("hashtag_patterns", [])
        rag_source       = rag_context.get("source", "fallback")

        if tone_rules:
            parts.append(f"[이 샵의 말투 패턴]\n{tone_rules}")

        if examples:
            ex_text = f"[과거 성과 좋은 게시물 분석 - {rag_source}]\n"
            ex_text += "마케터 관점: 이 게시물들이 왜 문의율이 높았는지 분석하고 패턴을 재현하세요.\n\n"
            for i, ex in enumerate(examples[:3], 1):
                caption  = ex.get("caption", "")
                hashtags = ex.get("hashtags", [])
                ex_text += f"{i}. {caption[:80]}{'...' if len(caption) > 80 else ''}\n"
                if hashtags:
                    ex_text += f"   해시태그: {' '.join(hashtags[:5])}\n"
                ex_text += f"   → 분석: 첫 문장 키워드 여부 / CTA 강도 / 타겟팅 명확성 체크\n"
            parts.append(ex_text)

        if hashtag_patterns:
            parts.append(f"[자주 쓰는 해시태그]\n{' '.join(hashtag_patterns[:10])}")

    # 4. 최근 게시물 말투 참고
    if recent_posts:
        recent_text = "[최근 게시물 말투 참고 (이 말투와 비슷하게)]\n"
        for i, post in enumerate(recent_posts[:2], 1):
            caption = post.get("caption", "")
            recent_text += f"{i}. {caption[:60]}{'...' if len(caption) > 60 else ''}\n"
        parts.append(recent_text)

    # 5. 재작성 시 피드백 추가
    if previous_draft and feedback:
        prev_caption = previous_draft.get("caption", "")
        parts.append(
            f"[이전 초안 - 수정 필요]\n{prev_caption}\n\n"
            f"[수정 요청]\n{feedback}\n\n"
            f"마케터 관점으로 재작성: 문의율 올리는 데 집중."
        )
    else:
        parts.append(
            "위 전략과 데이터를 바탕으로, 문의가 폭발하는 게시물을 작성하세요.\n"
            "체크리스트:\n"
            "✅ 첫 문장에 메인 키워드 배치\n"
            "✅ 타겟 고객 니즈 자극\n"
            "✅ 긴박감 있는 CTA\n"
            "✅ 검색량 높은 해시태그 우선 배치"
        )

    user_prompt = "\n\n".join(parts)
    return system_prompt, user_prompt


# [검증] 금칙어 자동 제거
def _validate_and_clean(result: dict, brand_settings: dict) -> dict:
    """
    금칙어 검증 + 자동 제거

    GPT가 금칙어를 포함했을 경우 자동으로 제거.
    완전 제거가 불가능한 경우 경고 로그만 출력.
    """
    # ✅ forbidden_words 리스트 처리
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]
    
    caption         = result.get("caption", "")
    found           = []

    for word in forbidden_words:
        if word in caption:
            found.append(word)
            caption = caption.replace(word, "")

    if found:
        print(f"[post_writer] ⚠️ 금칙어 발견 후 제거: {found}")
        result["caption"] = caption.strip()

    hashtags = result.get("hashtags", [])
    result["hashtags"] = [
        tag for tag in hashtags
        if not any(word in tag for word in forbidden_words)
    ]

    return result


# [Fallback] GPT 실패 시 기본 초안
def _fallback_draft(brand_settings: dict, trend_data: dict) -> dict:
    """
    GPT 호출 실패 시 기본 초안 반환.
    최소한의 내용으로 파이프라인이 멈추지 않게 유지.
    """
    cta   = brand_settings.get("cta", "DM으로 예약 문의주세요")
    trend = trend_data.get("trend", "")

    caption = "오늘도 깔끔한 스타일로 새로운 하루를 시작해보세요 ✂️"
    if trend:
        caption += f"\n{trend[:30]}"

    return {
        "caption":  caption,
        "hashtags": ["#바버샵", "#헤어스타일", "#남성헤어", "#페이드컷"],
        "cta":      cta
    }


# [커널 초기화]
def _init_kernel(tier: str = "mini") -> Kernel:
    """
    Semantic Kernel 초기화
    orchestrator에서 tier 결정 후 호출
    mini: GPT-4.1-mini (기본)
    full: GPT-4.1 (승격 시)
    """
    deployment = os.getenv(
        f"AZURE_OPENAI_DEPLOYMENT_{tier.upper()}",
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel