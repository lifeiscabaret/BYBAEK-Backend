"""
수동 채팅 라우터
- POST /api/custom_chat/manual_chat: 사장님 요청 → 트렌드 반영 캡션 즉시 생성 (스트리밍)

용도:
- 자동 파이프라인 외에 수동으로 게시물 캡션 즉시 생성
- "오늘 페이드컷으로 뭐라고 올려?" → 캡션 + 해시태그 + CTA 바로 출력
- web_search_agent 연결로 실시간 트렌드 반영
"""
import os
import json
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
from openai import AsyncAzureOpenAI

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

        return {
            "brand_tone":      shop.get("brand_tone", "친근하고 편안한 말투"),
            "forbidden_words": to_list(shop.get("forbidden_words")),
            "preferred_styles":to_list(shop.get("preferred_styles")),
            "cta":             shop.get("cta", "DM으로 예약 문의주세요"),
            "feed_style":      shop.get("feed_style", {}),
        }
    except Exception as e:
        print(f"[custom_chat] 브랜드 설정 조회 실패 (무시): {e}")
        return {}


async def generate_chat_stream(shop_id: str, message: str, photo_ids: List[str]):
    """
    트렌드 조회 → 브랜드 설정 조회 → 캡션 스트리밍 생성.
    출력: caption + hashtags + cta JSON 스트림.
    """
    endpoint   = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key    = os.getenv("AZURE_OPENAI_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or \
                 os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or \
                 os.getenv("AZURE_OPENAI_DEPLOYMENT")

    if not endpoint or not api_key or not deployment:
        yield "[❌ Azure OpenAI 환경변수가 설정되지 않았습니다.]"
        return

    # 1. 트렌드 + 브랜드 설정 병렬 조회
    import asyncio
    trend_data, brand_settings = await asyncio.gather(
        _get_trend_context(shop_id),
        _get_brand_settings(shop_id)
    )

    # 2. 브랜드 톤 처리
    brand_tone = brand_settings.get("brand_tone", "친근하고 편안한 말투")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)

    forbidden = brand_settings.get("forbidden_words", [])
    forbidden_str = ", ".join(forbidden) if forbidden else "없음"

    preferred = brand_settings.get("preferred_styles", [])
    preferred_str = ", ".join(preferred) if preferred else "페이드컷"

    feed_style    = brand_settings.get("feed_style", {})
    hashtag_count = feed_style.get("hashtag_count", 10)
    caption_len   = feed_style.get("caption_length", "2~4줄")

    # 3. 트렌드 컨텍스트
    trend_summary = trend_data.get("trend", "") or trend_data.get("trend_summary", "")
    weather       = trend_data.get("weather", "")
    promo         = trend_data.get("promo", "")
    cta           = brand_settings.get("cta") or "DM으로 예약 문의주세요"

    # 4. 시스템 프롬프트 — 캡션 JSON만 출력
    system_prompt = f"""너는 바버샵 인스타그램 게시물을 대신 써주는 마케터야.
사장님 요청을 받으면 캡션, 해시태그, CTA를 JSON으로만 출력해.

[절대 금지]
- 설명, 인사말, 분석, 팁 출력 금지
- "안녕하세요", "물론이죠", "아래는..." 같은 전치사 금지
- JSON 외 다른 텍스트 절대 금지
- 확인되지 않은 사실 지어내기 금지 (경력 연수, 예약 현황 등)

[브랜드 설정]
- 말투: {brand_tone}
- 전문 스타일: {preferred_str}
- 금칙어: {forbidden_str}
- 길이: {caption_len}
- 해시태그: {hashtag_count}개

[출력 형식 — 이것만]
{{
  "caption": "첫 문장에 스타일명 포함, {caption_len}, 자연스러운 말투",
  "hashtags": ["#페이드컷", "#바버샵", ... 총 {hashtag_count}개],
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

    # 6. 스트리밍 생성
    client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    )

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}
            ],
            stream=True,
            temperature=0.7,
            max_tokens=600
        )

        async for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    except Exception as e:
        print(f"[custom_chat] 스트리밍 오류: {e}")
        yield "\n\n[죄송합니다. 일시적인 오류가 발생했습니다. 다시 시도해주세요.]"


@router.post("/manual_chat")
async def manual_chat_agent(req: ManualChatRequest):
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
    return StreamingResponse(
        generate_chat_stream(req.shop_id, req.message, req.photo_ids),
        media_type="text/event-stream"
    )