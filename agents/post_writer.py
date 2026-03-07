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
    forbidden_str = ", ".join(brand_settings.get("forbidden_words", []))
    feed_style    = brand_settings.get("feed_style", {})
    emoji_usage   = feed_style.get("emoji_usage", "적당히")
    caption_len   = feed_style.get("caption_length", "2~4줄")
    hashtag_count = feed_style.get("hashtag_count", 10)

    system_prompt = f"""너는 경력 20년의 바버샵 전문 인스타그램 마케터야.
사장님의 브랜드 설정을 완벽하게 지키면서 인스타그램 게시물을 작성해줘.

[브랜드 설정]
- 말투/톤: {brand_settings.get("brand_tone", "친근하고 편안한 말투")}
- 절대 사용 금지 단어: {forbidden_str if forbidden_str else "없음"}
- CTA 문구: {brand_settings.get("cta", "DM으로 예약 문의주세요")}
- 이모지 사용: {emoji_usage}
- 캡션 길이: {caption_len}
- 해시태그 수: {hashtag_count}개 내외

[절대 규칙]
1. 금지 단어는 절대 사용하지 마
2. 여성 헤어, 펌, 염색 관련 내용 절대 금지
3. 과장되거나 거짓된 표현 금지 (예: "최고", "완벽한")
4. 반드시 JSON으로만 응답
5. 캡션 첫 줄에 반드시 스타일명 키워드 포함 (예: "페이드컷으로 봄을 맞이해요 🌿")
6. CTA는 문의/예약 행동을 직접적으로 유도하는 문구로 작성 (예: "이 스타일 궁금하면 DM 주세요 👇")

[응답 형식]
{{
  "caption": "캡션 내용 (줄바꿈은 \\n 사용)",
  "hashtags": ["#해시태그1", "#해시태그2", ...],
  "cta": "CTA 문구"
}}"""

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

    # ↓ 여기에 추가 (트렌드 블록 바로 다음)
    brand_diff = brand_settings.get("brand_differentiation", "")
    if brand_diff:
        parts.append(f"[우리 샵 차별점 - 반드시 언급]\n{brand_diff}")

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
            ex_text = f"[과거 게시물 예시 - {rag_source}]\n"
            for i, ex in enumerate(examples[:3], 1):
                caption  = ex.get("caption", "")
                hashtags = ex.get("hashtags", [])
                ex_text += f"{i}. {caption[:80]}{'...' if len(caption) > 80 else ''}\n"
                if hashtags:
                    ex_text += f"   해시태그: {' '.join(hashtags[:5])}\n"
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
            f"위 피드백을 반영해서 더 나은 캡션으로 재작성해줘."
        )
    else:
        parts.append("위 내용을 바탕으로 인스타그램 게시물을 작성해줘.")

    user_prompt = "\n\n".join(parts)
    return system_prompt, user_prompt


# [검증] 금칙어 자동 제거
def _validate_and_clean(result: dict, brand_settings: dict) -> dict:
    """
    금칙어 검증 + 자동 제거

    GPT가 금칙어를 포함했을 경우 자동으로 제거.
    완전 제거가 불가능한 경우 경고 로그만 출력.
    """
    forbidden_words = brand_settings.get("forbidden_words", [])
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


# [목업 테스트] 단독 실행용
if __name__ == "__main__":
    import asyncio
    from dotenv import load_dotenv
    load_dotenv()

    mock_trend = {
        "trend": "2026년 봄 페이드컷(fade cut)과 사이드파트(side part) 인기 상승 중",
        "weather": "맑음 18도, 봄 시즌",
        "promo": "봄 신규 고객 이벤트"
    }

    mock_photos = [
        {"id": "photo_001", "style_tags": ["fade_cut", "side_part"]},
        {"id": "photo_002", "style_tags": ["slick_back"]}
    ]

    mock_brand = {
        "brand_tone": "친근하고 편안한 말투",
        "forbidden_words": ["저렴", "할인"],
        "cta": "DM으로 예약 문의주세요",
        "feed_style": {
            "emoji_usage": "자주",
            "caption_length": "2~4줄",
            "hashtag_count": 10
        }
    }

    mock_recent_posts = [
        {
            "caption": "봄이 왔어요! 새로운 스타일로 변신해볼까요? ✂️",
            "hashtags": ["#바버샵", "#페이드컷", "#봄헤어"]
        }
    ]

    # RAG Fallback 컨텍스트 목업
    mock_rag = {
        "examples": [
            {
                "caption": "깔끔한 페이드컷으로 봄을 맞이해요 🌿",
                "hashtags": ["#바버샵", "#페이드컷"]
            }
        ],
        "tone_rules": "친근한 말투, 이모지 자주 사용",
        "hashtag_patterns": ["#바버샵", "#페이드컷", "#남성헤어"],
        "cta_pattern": "DM으로 예약 문의주세요",
        "source": "fallback"
    }

    async def test():
        print("=" * 50)
        print("[테스트 1] 최초 작성")
        print("=" * 50)
        result = await post_writer_agent(
            shop_id="shop_test_001",
            trend_data=mock_trend,
            selected_photos=mock_photos,
            brand_settings=mock_brand,
            recent_posts=mock_recent_posts,
            rag_context=mock_rag
        )
        print(f"\n[결과]")
        print(f"  캡션:     {result['caption']}")
        print(f"  해시태그: {result['hashtags']}")
        print(f"  CTA:      {result['cta']}")

        print("\n" + "=" * 50)
        print("[테스트 2] 재작성 (피드백 반영)")
        print("=" * 50)
        result2 = await post_writer_agent(
            shop_id="shop_test_001",
            trend_data=mock_trend,
            selected_photos=mock_photos,
            brand_settings=mock_brand,
            recent_posts=mock_recent_posts,
            rag_context=mock_rag,
            previous_draft=result,
            feedback="브랜드 톤 점수 0.65 미달. 더 친근한 말투로 재조정 필요."
        )
        print(f"\n[재작성 결과]")
        print(f"  캡션:     {result2['caption']}")
        print(f"  해시태그: {result2['hashtags']}")
        print(f"  CTA:      {result2['cta']}")

    asyncio.run(test())