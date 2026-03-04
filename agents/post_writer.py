import os
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from semantic_kernel.connectors.ai.open_ai import OpenAIChatPromptExecutionSettings

MIN_CAPTION_LEN = 30
MAX_CAPTION_LEN = 500
MIN_HASHTAG_COUNT = 5


def _now_kst() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _init_kernel(tier: str = "mini") -> Kernel:
    """
    티어별 Kernel 초기화
    tier="mini" → DEPLOYMENT_MINI
    tier="full" → DEPLOYMENT_FULL (orchestrator Self-Eval 소진 시 승격)
    """
    key = f"AZURE_OPENAI_DEPLOYMENT_{tier.upper()}"
    deployment = os.getenv(key, os.getenv("AZURE_OPENAI_DEPLOYMENT"))
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel

def _format_photo_meta(photos: list[dict]) -> str:
    if not photos:
        return "사진 정보 없음"
    lines = []
    for i, p in enumerate(photos[:5], 1):
        tags = ", ".join(p.get("style_tags", [])) or "태그 없음"
        score = p.get("fade_cut_score", 0)
        lines.append(f"  사진{i}: 스타일=[{tags}], 페이드컷점수={score:.2f}")
    return "\n".join(lines)


def _format_recent_posts(posts: list[dict]) -> str:
    if not posts:
        return "이전 게시물 없음 (신규 샵)"
    return "\n".join(
        f'  - "{p.get("caption", "")[:100]}..."'
        for p in posts[:3]
    )


def _format_rag_context(rag: dict) -> str:
    if not rag:
        return "RAG 예시 없음"
    tone = rag.get("tone_rules", "")
    examples = rag.get("examples", [])
    if not examples:
        return tone or "RAG 예시 없음"
    ex_text = "\n".join(
        f'  예시{i+1}: "{ex[:120]}..."'
        for i, ex in enumerate(examples[:3])
    )
    return f"톤 규칙: {tone}\n좋은 예시:\n{ex_text}"


def _build_prompt(
    trend_data: dict,
    selected_photos: list[dict],
    brand_settings: dict,
    recent_posts: list[dict],
    rag_context: dict,
    previous_draft: Optional[dict],
    feedback: Optional[str]
) -> str:
    today = _now_kst().strftime("%Y년 %m월 %d일")
    is_rewrite = previous_draft is not None and feedback is not None

    tone = brand_settings.get("brand_tone", "친근하고 전문적인 말투")
    forbidden = brand_settings.get("forbidden_words", [])
    cta = brand_settings.get("cta", "DM으로 예약 문의주세요")
    feed_style = brand_settings.get("feed_style", {})
    hashtag_count = feed_style.get("hashtag_count", 10)
    emoji_usage = feed_style.get("emoji_usage", "보통")
    caption_length = feed_style.get("caption_length", "중간")
    shop_vibe = brand_settings.get("shop_vibe", "전문 남성 바버샵")
    specialty = brand_settings.get("specialty_styles", [])

    forbidden_str = ", ".join(f'"{w}"' for w in forbidden) if forbidden else "없음"
    specialty_str = ", ".join(specialty) if specialty else "미지정"
    length_guide = {
        "짧고 간결": "2~3문장, 50자 이하",
        "중간": "3~5문장, 100~200자",
        "길게": "5~7문장, 200~400자"
    }.get(caption_length, "3~5문장, 100~200자")

    rewrite_section = ""
    if is_rewrite:
        rewrite_section = f"""
━━━ 재작성 요청 ━━━
이전 캡션: {previous_draft.get("caption", "")}
이전 해시태그: {" ".join(previous_draft.get("hashtags", []))}
개선 피드백: {feedback}
위 피드백을 반드시 반영하여 더 나은 버전을 작성하세요.
━━━━━━━━━━━━━━━━━━
"""

    return f"""
당신은 한국 바버샵 전문 SNS 마케터입니다.
오늘 인스타그램에 올릴 게시물을 작성하세요.

━━━ 기본 정보 ━━━
오늘 날짜: {today}
샵 분위기: {shop_vibe}
전문 스타일: {specialty_str}

━━━ 브랜드 설정 ━━━
말투/톤: {tone}
이모지 사용: {emoji_usage}
캡션 길이: {length_guide}
금칙어 (절대 사용 금지): {forbidden_str}
CTA: {cta}

━━━ 오늘의 트렌드 ━━━
트렌드: {trend_data.get("trend", "")}
날씨: {trend_data.get("weather", "")}
홍보 포인트: {trend_data.get("promo", "")}

━━━ 오늘 선택된 사진 ━━━
{_format_photo_meta(selected_photos)}

━━━ 최근 게시물 말투 샘플 ━━━
{_format_recent_posts(recent_posts)}

━━━ RAG 컨텍스트 ━━━
{_format_rag_context(rag_context)}
{rewrite_section}
━━━ 작성 규칙 ━━━
1. 브랜드 말투 정확히 따를 것
2. 금칙어({forbidden_str}) 절대 사용 금지
3. 사진 스타일 태그를 자연스럽게 반영
4. 해시태그 정확히 {hashtag_count}개 (# 포함)
5. 트렌드/날씨를 자연스럽게 녹일 것

다음 JSON 형식으로만 응답 (마크다운 없이):
{{"caption": "본문", "hashtags": ["#태그1", "#태그2"], "cta": "CTA문구"}}
""".strip()


def _parse_json(text: str) -> dict:
    """GPT 응답에서 JSON 파싱. 마크다운 제거 후 처리."""
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        return json.loads(cleaned)
    except Exception as e:
        print(f"[post_writer] JSON 파싱 실패 ({e})")
        return {}


def _validate(result: dict, brand_settings: dict) -> tuple[bool, str]:
    """기본 검증: 구조/길이/금칙어"""
    caption = result.get("caption", "")
    hashtags = result.get("hashtags", [])
    issues = []

    if not caption:
        issues.append("캡션이 비어있습니다.")
    elif len(caption) < MIN_CAPTION_LEN:
        issues.append(f"캡션이 너무 짧습니다 ({len(caption)}자).")
    elif len(caption) > MAX_CAPTION_LEN:
        issues.append(f"캡션이 너무 깁니다 ({len(caption)}자).")

    if len(hashtags) < MIN_HASHTAG_COUNT:
        issues.append(f"해시태그 부족 ({len(hashtags)}개).")

    for word in brand_settings.get("forbidden_words", []):
        if word in caption:
            issues.append(f'금칙어 "{word}" 포함.')

    return (not issues), " / ".join(issues)


def _fallback(brand_settings: dict) -> dict:
    """GPT 완전 실패 시 최소 fallback"""
    cta = brand_settings.get("cta", "DM으로 예약 문의주세요")
    specialty = brand_settings.get("specialty_styles", ["페이드컷"])
    style = specialty[0] if specialty else "헤어스타일"
    month = _now_kst().strftime("%-m")
    return {
        "caption": (
            f"{month}월의 새로운 {style}, 지금 이 순간을 담았습니다. "
            "완성도 높은 시술로 자신감을 더해보세요."
        ),
        "hashtags": [
            "#바버샵", "#남성헤어", f"#{style.replace(' ', '')}",
            "#헤어스타일", "#바버", "#헤어컷",
            "#남자헤어", "#fade", "#barbershop", "#haircut"
        ],
        "cta": cta
    }

async def post_writer_agent(
    shop_id: str,
    trend_data: dict,
    selected_photos: list[dict],
    brand_settings: dict,
    recent_posts: list[dict],
    rag_context: dict,
    previous_draft: Optional[dict] = None,
    feedback: Optional[str] = None,
    tier: str = "mini"
) -> dict:
    """
    게시물 작성 에이전트 메인 함수.
    orchestrator STEP 5에서 호출됨.

    Args:
        shop_id:         샵 고유 ID
        trend_data:      web_search_agent 결과
        selected_photos: photo_select_agent가 선택한 사진 메타 리스트
        brand_settings:  브랜드/온보딩 설정
        recent_posts:    최근 게시물 3개
        rag_context:     rag_tool 결과
        previous_draft:  orchestrator 재작성 요청 시 이전 초안 (없으면 None)
        feedback:        재작성 피드백 (없으면 None)
        tier:            "mini" | "full" (orchestrator에서 결정)

    Returns:
        {"caption": str, "hashtags": list[str], "cta": str}
    """
    is_rewrite = previous_draft is not None and feedback is not None
    print(f"[post_writer] 시작 (tier={tier}, rewrite={is_rewrite}, "
          f"photos={len(selected_photos)}장)")

    # 프롬프트 구성
    prompt = _build_prompt(
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts,
        rag_context=rag_context,
        previous_draft=previous_draft,
        feedback=feedback
    )

    # GPT 호출
    try:
        kernel = _init_kernel(tier)
        chat_service = kernel.get_service("azure_openai")
        chat_history = ChatHistory()
        chat_history.add_user_message(prompt)
        settings = OpenAIChatPromptExecutionSettings(temperature=0.7, max_tokens=900)
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=settings,
            kernel=kernel
        )
        result = _parse_json(str(response).strip())
    except Exception as e:
        print(f"[post_writer] GPT 호출 실패 ({e}) → fallback")
        return _fallback(brand_settings)

    # 구조 검증
    if not result.get("caption"):
        print("[post_writer] 응답 구조 오류 → fallback")
        return _fallback(brand_settings)

    # hashtags 타입 보정
    hashtags = result.get("hashtags", [])
    if isinstance(hashtags, str):
        hashtags = [h.strip() for h in hashtags.split() if h.startswith("#")]
    hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags]
    result["hashtags"] = hashtags

    # CTA fallback
    if not result.get("cta"):
        result["cta"] = brand_settings.get("cta", "DM으로 예약 문의주세요")

    # 기본 검증
    passed, validation_msg = _validate(result, brand_settings)
    if not passed:
        print(f"[post_writer] 검증 실패: {validation_msg}")

    print(f"[post_writer] 완료 → caption={len(result.get('caption',''))}자, "
          f"hashtags={len(result.get('hashtags', []))}개")
    return result