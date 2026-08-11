"""
수동 채팅 라우터
- POST /api/custom_chat/manual_chat: 사장님 요청 → 트렌드 반영 캡션 즉시 생성 (스트리밍)

용도:
- 자동 파이프라인 외에 수동으로 게시물 캡션 즉시 생성
- "오늘 페이드컷으로 뭐라고 올려?" → 캡션 + 해시태그 + CTA 바로 출력
- web_search_agent 연결로 실시간 트렌드 반영
"""
import json
import re
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
import anthropic

from auth.token_verify import get_current_shop, require_shop_owner
from utils.claude_auth import CLAUDE_BASE_URL, get_claude_model, get_claude_token


router = APIRouter()


class ManualChatRequest(BaseModel):
    shop_id: str
    message: str
    photo_ids: List[str] = []

    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "message": "오늘 페이드컷으로 인스타그램 게시물 만들어줘",
                "photo_ids": []
            }
        }


async def _get_trend_context(shop_id: str) -> dict:
    """
    web_search_agent로 오늘 트렌드 조회.
    실패 시 빈 dict 반환 (캡션 생성은 계속 진행).
    """
    try:
        from agents.web_search import web_search_agent
        trend = await web_search_agent(shop_id)
        return trend
    except Exception as e:
        print(f"[custom_chat] 트렌드 조회 실패 (무시): {e}")
        return {}


async def _get_brand_settings(shop_id: str) -> dict:
    """CosmosDB에서 브랜드 설정 조회. 실패 시 기본값 반환."""
    try:
        from services.cosmos_db import get_onboarding
        data = get_onboarding(shop_id)
        if not data:
            return {}
        shop = data.get("shop_info", {})

        def to_list(val):
            if isinstance(val, list): return val
            if isinstance(val, str) and val: return [v.strip() for v in val.split(",")]
            return []

        # brand_tone 배열에서 이모지 사용 여부 파싱
        brand_tone_list = to_list(shop.get("brand_tone"))
        emoji_map = {"자주 씀": "자주", "가끔 씀": "가끔", "안 씀": "안 씀"}
        emoji_usage = next(
            (emoji_map[v] for v in brand_tone_list if v in emoji_map),
            "가끔"
        )

        return {
            "brand_tone":            brand_tone_list,
            "forbidden_words":       to_list(shop.get("forbidden_words")),
            "preferred_styles":      to_list(shop.get("preferred_styles")),
            "exclude_conditions":    to_list(shop.get("exclude_conditions")),
            "hashtag_style":         to_list(shop.get("hashtag_style")),
            "cta":                   shop.get("cta", "DM으로 예약 문의주세요"),
            "shop_intro":            shop.get("shop_intro", ""),
            "language":              shop.get("language", "ko"),
            "feed_style": {
                "emoji_usage":    emoji_usage,
                "caption_length": shop.get("caption_length", "2~4줄"),
                "hashtag_count":  shop.get("hashtag_count", 10),
            },
        }
    except Exception as e:
        print(f"[custom_chat] 브랜드 설정 조회 실패 (무시): {e}")
        return {}


async def generate_chat_stream(shop_id: str, message: str, photo_ids: List[str]):
    """
    트렌드 조회 → 브랜드 설정 조회 → 캡션 스트리밍 생성.
    출력: caption + hashtags + cta JSON 스트림.
    """
    # Claude 인증: Azure Foundry /anthropic 은 AAD(Entra) 토큰만 허용 (api-key 미지원)
    try:
        claude_token = get_claude_token()
    except Exception as e:
        yield f"[❌ Claude AAD 토큰 발급 실패: {e}]"
        return

    # 1. 트렌드 + 브랜드 설정 병렬 조회
    import asyncio
    trend_data, brand_settings = await asyncio.gather(
        _get_trend_context(shop_id),
        _get_brand_settings(shop_id)
    )

    # 2. 브랜드 톤 처리
    brand_tone = brand_settings.get("brand_tone", ["친근하고 편안한 말투"])
    if isinstance(brand_tone, list):
        # 스타일/타겟/이모지 항목 분리해서 프롬프트에 명확하게 전달
        style_labels = ["힙/스트릿 바이브", "클래식 프리미엄", "친근한 동네 바버", "감성/무드"]
        emoji_labels = ["자주 씀", "가끔 씀", "안 씀"]
        tone_items = [v for v in brand_tone if v not in emoji_labels]
        brand_tone = " / ".join(tone_items) if tone_items else "친근하고 편안한 말투"

    forbidden = brand_settings.get("forbidden_words", [])
    forbidden_str = ", ".join(forbidden) if forbidden else "없음"

    preferred = brand_settings.get("preferred_styles", [])
    preferred_str = ", ".join(preferred) if preferred else "페이드컷"

    feed_style    = brand_settings.get("feed_style", {})
    hashtag_count = feed_style.get("hashtag_count", 10)
    caption_len   = feed_style.get("caption_length", "2~4줄")

    # 출력 언어 지시 (레이어1: post_writer와 동일 로직. LANG_NAMES는 일단 각자 정의 — 공통 모듈화는 별도 리팩토링)
    language = brand_settings.get("language", "ko")
    LANG_NAMES = {"ko": "한국어", "en": "English", "ja": "日本語",
                  "zh": "中文", "es": "Español"}
    lang_name = LANG_NAMES.get(language, language)
    if language != "ko":
        lang_instruction = f"\n\n[출력 언어 — 매우 중요]\n반드시 {lang_name}로 캡션과 해시태그, CTA를 작성해줘. 자연스러운 {lang_name} 표현으로 쓰되, 바버샵 정체성과 마케팅 의도는 그대로 유지해."
    else:
        lang_instruction = ""

    # 3. 트렌드 컨텍스트 + 브랜드 추가 설정
    trend_summary = trend_data.get("trend", "") or trend_data.get("trend_summary", "")
    weather       = trend_data.get("weather", "")
    promo         = trend_data.get("promo", "")
    cta           = brand_settings.get("cta") or "DM으로 예약 문의주세요"
    shop_intro    = brand_settings.get("shop_intro", "")
    exclude_conditions = brand_settings.get("exclude_conditions", [])
    exclude_str   = ", ".join(exclude_conditions) if exclude_conditions else "없음"
    # 필수 해시태그: hashtag_style 에서 #로 시작하는 항목 추출 (must_include_hashtags 필드 폐지)
    # 공백 join — 콤마 나열 패턴을 LLM이 흉내내 출력 해시태그에 콤마 붙는 것 방지
    hashtag_style = brand_settings.get("hashtag_style", [])
    must_hashtags = [t for t in hashtag_style if t.startswith("#")]
    must_hashtag_str = " ".join(must_hashtags) if must_hashtags else "없음"

    # 4. 시스템 프롬프트 — 캡션 JSON만 출력
    system_prompt = f"""너는 바버샵 인스타그램 게시물을 대신 써주는 마케터야.
사장님 요청을 받으면 캡션, 해시태그, CTA를 JSON으로만 출력해.

[절대 금지]
- 설명, 인사말, 분석, 팁 출력 금지
- "안녕하세요", "물론이죠", "아래는..." 같은 전치사 금지
- JSON 외 다른 텍스트 절대 금지
- 확인되지 않은 사실 지어내기 금지 (경력 연수, 예약 현황 등)
- 해시태그에 콤마(,) 붙이기 금지 — 배열 항목으로만 분리

[브랜드 설정]
- 말투: {brand_tone}
- 전문 스타일: {preferred_str}
- 금칙어: {forbidden_str}
- 언급 금지: {exclude_str}
- 길이: {caption_len}
- 해시태그: {hashtag_count}개
- 필수 해시태그 (반드시 포함): {must_hashtag_str}
{f"[샵 소개 - 이 내용은 사실이므로 캡션에 자연스럽게 활용 가능]{chr(10)}{shop_intro}" if shop_intro else ""}{lang_instruction}

[출력 형식 — 이것만]
{{
  "caption": "첫 문장에 스타일명 포함, {caption_len}, 자연스러운 말투",
  "hashtags": ["#페이드컷", "#바버샵", ... 총 {hashtag_count}개 (각 항목은 #로 시작, 콤마·공백 없이 개별 문자열로)],
  "cta": "예약 유도 문구"
}}"""

    # 5. 유저 프롬프트 — 트렌드 + 사장님 요청
    user_parts = []
    if trend_summary:
        user_parts.append(f"[오늘 트렌드]\n{trend_summary}")
    if weather:
        user_parts.append(f"[날씨/시즌]\n{weather}")
    if promo:
        user_parts.append(f"[홍보 포인트]\n{promo}")
    user_parts.append(f"[사장님 요청]\n{message}")
    user_parts.append(f"위 내용 반영해서 인스타 게시물 JSON만 출력해. CTA는 \"{cta}\" 스타일로.")

    user_prompt = "\n\n".join(user_parts)

    # 6. 스트리밍 생성 (Claude — 모델은 CLAUDE_MODEL_NAME 환경변수)
    client = anthropic.Anthropic(
        base_url=CLAUDE_BASE_URL,
        auth_token=claude_token
    )

    try:
        with client.messages.stream(
            model=get_claude_model(),
            max_tokens=600,
            temperature=0.7,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        ) as stream:
            full = "".join(stream.text_stream)  # 전체 수신 후 마크다운 펜스 제거

        # Claude가 JSON을 ```json ... ``` 마크다운 펜스로 감싸는 경우 제거.
        # (post_writer/orchestrator와 동일한 처리 — 프론트가 순수 JSON을 파싱하도록 보장)
        cleaned = re.sub(r"^\s*```(?:json)?\s*", "", full.strip())
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
        yield cleaned

    except Exception as e:
        print(f"[custom_chat] 스트리밍 오류: {e}")
        yield "\n\n[죄송합니다. 일시적인 오류가 발생했습니다. 다시 시도해주세요.]"


@router.post("/manual_chat")
async def manual_chat_agent(req: ManualChatRequest, current_shop: dict = Depends(get_current_shop)):
    """
    사장님 요청 → 트렌드 반영 캡션 스트리밍 생성

    Returns:
        StreamingResponse: JSON 형식 캡션 스트림

    Usage:
        ```javascript
        const response = await fetch('/api/custom_chat/manual_chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({shop_id: "3sesac18", message: "페이드컷 게시물 만들어줘"})
        });
        const reader = response.body.getReader();
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            console.log(new TextDecoder().decode(value));
        }
        ```
    """
    require_shop_owner(current_shop, req.shop_id)
    return StreamingResponse(
        generate_chat_stream(req.shop_id, req.message, req.photo_ids),
        media_type="text/event-stream"
    )