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

        # 금칙어 + 할루시네이션 검증
        result = _validate_and_clean(result, brand_settings)

        # 할루시네이션 감지 시 한 번 재시도
        if result.get("needs_retry"):
            reason = result.get("retry_reason", "할루시네이션")
            print(f"[post_writer] {reason} 감지 → 재시도 (feedback 주입)")
            feedback_msg = f"이전 캡션에서 '{reason}'이 감지됐어. 확인되지 않은 사실은 절대 쓰지 마."
            chat_history.add_assistant_message(str(result.get("caption", "")))
            chat_history.add_user_message(feedback_msg)
            response2 = await chat_service.get_chat_message_content(
                chat_history=chat_history,
                settings=chat_service.instantiate_prompt_execution_settings()
            )
            raw2 = str(response2).strip().replace("```json", "").replace("```", "").strip()
            try:
                result = _validate_and_clean(json.loads(raw2), brand_settings)
            except Exception:
                pass  # 재시도도 실패하면 원본 그대로 사용

        result.pop("needs_retry", None)
        result.pop("retry_reason", None)

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

    # AG-040: 온보딩 추가 필드
    hashtag_style = brand_settings.get("hashtag_style", "감성형")

    preferred_styles = brand_settings.get("preferred_styles", [])
    if isinstance(preferred_styles, str):
        preferred_styles = [s.strip() for s in preferred_styles.split(",") if s.strip()]
    preferred_str = ", ".join(preferred_styles) if preferred_styles else "페이드컷, 투블럭 등 바버샵 스타일"

    exclude_conditions = brand_settings.get("exclude_conditions", [])
    if isinstance(exclude_conditions, str):
        exclude_conditions = [s.strip() for s in exclude_conditions.split(",") if s.strip()]
    exclude_str = ", ".join(exclude_conditions) if exclude_conditions else "없음"

    # 시스템 프롬프트 구성
    system_prompt = f"""너는 한국 바버샵 인스타그램 게시물을 대신 써주는 마케터야.
사장님은 시술로 바빠서 직접 홍보할 시간이 없어. 네가 대신 써줘야 해.

[작성 원칙]
1. 절대로 확인되지 않은 정보를 지어내지 마
   - 경력 연수 (예: "22년 경력", "10년 경력") → 절대 금지. DB에 없으면 쓰지 마
   - 예약 현황 (예: "오늘 3자리 남음", "마감 임박") → 절대 금지. 실제 현황 모름
   - 수상 이력, 인증서, 특허 → 절대 금지

2. 자연스러운 사람 말투로 써
   - AI티 나는 표현 금지: "정교한 그라데이션으로 완성하는", "트렌디한 스타일을 선사하는"
   - 실제 바버샵 사장님이 쓸 법한 말투로: 짧고, 직접적으로
   - 이모지: {emoji_usage}
   - 길이: {caption_len}

3. 이 샵 스타일 범위 안에서만 작성
   - 전문 스타일: {preferred_str}
   - 금칙어: {forbidden_str}
   - 언급 금지 조건: {exclude_str}

4. 브랜드 톤: {brand_tone}

5. 해시태그: {hashtag_style} 스타일 / 총 {hashtag_count}개

[출력 형식 - JSON만, 설명 없이]
{{
  "caption": "첫 문장에 페이드 스타일명 포함\\n2~3줄, 자연스러운 말투",
  "hashtags": ["#페이드컷", "#바버샵", ... 총 {hashtag_count}개],
  "cta": "예약 유도 문구 (예: DM 주시면 바로 확인해드려요)"
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

    # 샵 차별점 - shop_intro 있을 때만 반영 (없으면 GPT가 지어내므로 금지)
    brand_diff = brand_settings.get("brand_differentiation", "").strip()
    if brand_diff:
        parts.append(f"[우리 샵 차별점 - 첫 문장에 자연스럽게 녹여줘]\n{brand_diff}")

    # 실제 검색 스니펫 - 사람들이 실제로 쓰는 말투 참고용
    raw_snippets = trend_data.get("raw_snippets", [])
    if raw_snippets:
        snippet_text = "\n".join(f"- {s}" for s in raw_snippets[:3])
        parts.append(f"[실제 검색에서 수집한 표현 - 말투 참고만, 그대로 복붙 금지]\n{snippet_text}")

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


# AG-042: 할루시네이션 방지 - 바버샵 무관 주제 키워드
_FORBIDDEN_TOPICS = [
    "레이어컷", "펌", "염색", "여성", "헤어숍", "미용실",
    "네일", "왁싱", "속눈썹", "피부", "스킨케어",
]

# AG-042: 과장 표현 금지
_FORBIDDEN_EXAGGERATIONS = [
    "최고의", "완벽한", "세계 최초", "혁신적인", "압도적인",
    "독보적인", "전국 1위", "업계 최고",
]


# [검증] 금칙어 + 할루시네이션 자동 제거
def _validate_and_clean(result: dict, brand_settings: dict) -> dict:
    """
    AG-042 강화: 금칙어 + 주제 이탈 + 과장 표현 3중 검사

    1) 금칙어 (브랜드 설정)  → 자동 제거
    2) 주제 이탈 키워드      → 자동 제거 + 경고
    3) 과장 표현             → 자동 제거 + 경고
    """
    # forbidden_words 리스트 처리
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]

    caption = result.get("caption", "")

    # 0) 할루시네이션 패턴 감지 → 재생성 신호 (제거 말고 플래그)
    import re
    hallucination_patterns = [
        (r'\d+년\s*경력',   "경력 연수 할루시네이션"),
        (r'\d+자리\s*남',   "예약 현황 할루시네이션"),
        (r'마감\s*임박',     "마감 임박 할루시네이션"),
        (r'오늘만\s*할인',   "근거없는 할인 할루시네이션"),
    ]
    for pattern, label in hallucination_patterns:
        if re.search(pattern, caption):
            print(f"[post_writer] ⚠️  {label} 감지 → needs_retry=True")
            result["needs_retry"] = True
            result["retry_reason"] = label
            return result

    # 1) 금칙어 제거
    found_forbidden = []
    for word in forbidden_words:
        if word in caption:
            found_forbidden.append(word)
            caption = caption.replace(word, "")
    if found_forbidden:
        print(f"[post_writer] AG-042 금칙어 제거: {found_forbidden}")

    # 2) 주제 이탈 제거
    found_topics = []
    for word in _FORBIDDEN_TOPICS:
        if word in caption:
            found_topics.append(word)
            caption = caption.replace(word, "")
    if found_topics:
        print(f"[post_writer] AG-042 주제 이탈 키워드 제거: {found_topics}")

    # 3) 과장 표현 제거
    found_exaggerations = []
    for word in _FORBIDDEN_EXAGGERATIONS:
        if word in caption:
            found_exaggerations.append(word)
            caption = caption.replace(word, "")
    if found_exaggerations:
        print(f"[post_writer] AG-042 과장 표현 제거: {found_exaggerations}")

    result["caption"] = caption.strip()

    # 해시태그에서도 금칙어 + 주제 이탈 제거
    all_banned = forbidden_words + _FORBIDDEN_TOPICS
    hashtags = result.get("hashtags", [])
    result["hashtags"] = [
        tag for tag in hashtags
        if not any(word in tag for word in all_banned)
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