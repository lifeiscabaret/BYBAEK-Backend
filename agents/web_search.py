import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from tavily import TavilyClient


LOCALE_CONFIG = {
    "KR": {
        "language": "Korean",
        "timezone_offset": 9,
        "timezone_name": "KST",
        "weather_query": "{city} 오늘 날씨",
        "trend_queries": [
            "2026 바버샵 남성 페이드컷 헤어스타일 트렌드 추천 한국",
            "2026 barbershop men fade cut haircut sidepart trend Korea"
        ],
        "seasons": {
            (3, 4, 5): "봄",
            (6, 7, 8): "여름",
            (9, 10, 11): "가을",
            (12, 1, 2): "겨울"
        },
        "season_fallback": {
            "봄": "봄 시즌 새 학기 페이드컷으로 깔끔하게 변화 적기",
            "여름": "여름 시원한 스킨페이드, 크루컷 추천 시즌",
            "가을": "가을 분위기 슬릭백, 아이비리그컷 변화 시즌",
            "겨울": "연말 특별한 포마드컷, 리젠트로 스타일 변화 시즌"
        },
        "weather_prompt": (
            "아래 날씨 정보를 '맑음, 6도, 봄바람' 형식으로 최대 두 줄로 요약해줘. "
            "요약만 출력해."
        ),
        "trend_prompt": """아래 검색 결과에서 바버샵 남성 헤어스타일 트렌드를 분석하고
마케팅 활용 전략까지 제시해줘.

[분석 목표]
- 고객 1순위 니즈: 페이드컷
- 타겟: 20-40대 남성 (직장인/대학생)
- 최종 목표: 예약 문의 폭발

[바버샵 전용 스타일만 포함]
페이드컷 (최우선), 사이드파트, 슬릭백, 아이비리그, 포마드,
버즈컷, 크루컷, 크롭컷, 투블럭, 멀릿 등

[절대 금지]
여성 헤어, 펌, 염색, 미용실 관련 내용

검색 결과:
{raw_trend}

아래 JSON 형식으로만 응답해줘. 설명/마크다운 없이 JSON만:
{{
  "trend_summary": "어떤 스타일이 인기인가? 페이드컷 중심 2줄 이내",
  "target_analysis": "어떤 고객층이 이 트렌드를 찾는가? 1줄",
  "marketing_strategy": "이 트렌드로 어떻게 예약 문의를 유도할 수 있는가? 1줄"
}}
""",
        "promo_prompt": (
            "오늘은 {today}이고 계절은 {season}이야.\n"
            "트렌드 요약: {trend_summary}\n"
            "타겟 고객: {target_analysis}\n\n"
            "위 정보를 바탕으로 바버샵 인스타그램 게시물에 쓸 수 있는 "
            "계절감 + 트렌드가 담긴 홍보 포인트를 1~2줄로 만들어줘. "
            "페이드컷 중심, 예약 긴박감 포함. "
            "홍보 포인트만 출력해."
        )
    }
}


def _init_kernel() -> Kernel:
    deployment = os.getenv(
        "AZURE_OPENAI_DEPLOYMENT_MINI",
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


def _init_tavily() -> TavilyClient:
    return TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))


def _parse_json_safe(text: str) -> dict:
    """
    AG-001: GPT JSON 응답 안전 파싱.
    마크다운 코드블록 제거 후 파싱, 실패 시 빈 dict 반환.
    """
    text = str(text).strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1)
    try:
        return json.loads(text)
    except Exception as e:
        print(f"[web_search] JSON 파싱 실패 ({e}) → fallback 사용")
        return {}


async def _get_shop_info(shop_id: str) -> dict:
    from services.cosmos_db import get_shop_location
    try:
        return get_shop_location(shop_id)
    except Exception as e:
        print(f"[web_search] shop_location 조회 실패 ({e}) → 기본값 사용")
        return {"locale": "KR", "city": "서울"}


async def _get_cache(shop_id: str, date_str: str) -> dict | None:
    from services.cosmos_db import get_today_web_search_cache
    try:
        return get_today_web_search_cache(shop_id, date_str)
    except Exception as e:
        print(f"[web_search] 캐시 조회 실패 ({e}) → 캐시 없음으로 처리")
        return None


async def _save_cache(shop_id: str, result: dict, date_str: str) -> None:
    from services.cosmos_db import save_web_search_cache
    try:
        save_web_search_cache(shop_id, date_str, result)
    except Exception as e:
        print(f"[web_search] 캐시 저장 실패 ({e}) → 무시하고 계속")


async def web_search_agent(shop_id: str, force_refresh: bool = False) -> dict:
    """
    웹 서치 에이전트 메인 함수

    출력:
    {
        "weather": "맑음, 6도, 봄바람",
        "trend": "페이드컷과 사이드파트가...",
        "target": "깔끔한 이미지 원하는 직장인...",
        "strategy": "봄 이미지 변신 지금...",
        "promo": "봄 트렌드 페이드컷 변화 적기, 이번 주 예약 마감 임박",
        ...
    }
    """
    shop_info = await _get_shop_info(shop_id)
    locale    = shop_info.get("locale", "KR")
    city      = shop_info.get("city", "서울")
    config    = LOCALE_CONFIG[locale]

    tz_offset = timedelta(hours=config["timezone_offset"])
    local_tz  = timezone(tz_offset)
    now       = datetime.now(local_tz)
    today_str = now.strftime("%Y-%m-%d")

    if not force_refresh:
        cached = await _get_cache(shop_id, today_str)
        if cached:
            print(f"[web_search] 캐시 히트 → shop_id={shop_id}, date={today_str}")
            return cached
    else:
        print(f"[web_search] force_refresh=True → 캐시 무시하고 재검색")

    kernel = _init_kernel()
    tavily = _init_tavily()

    # 날씨 + 트렌드 병렬 수집
    (weather, weather_sources), (trend_data, trend_sources) = await asyncio.gather(
        _get_weather(tavily, kernel, city, now, config),
        _get_barbershop_trend(tavily, kernel, config)
    )

    promo = await _get_promo_info(kernel, now, config, trend_data)

    result = {
        "weather":         weather,
        "weather_sources": weather_sources,
        "trend":           trend_data.get("trend_summary", ""),
        "target":          trend_data.get("target_analysis", ""),
        "strategy":        trend_data.get("marketing_strategy", ""),
        "trend_sources":   trend_sources,
        "promo":           promo,
        "locale":          locale,
        "city":            city,
        "collected_at":    now.isoformat()
    }

    await _save_cache(shop_id, result, today_str)
    print(f"[web_search] 완료 → trend={result['trend'][:40]}...")
    return result


async def _get_weather(tavily, kernel, city, now, config) -> tuple:
    """날씨 정보 수집"""
    try:
        query    = config["weather_query"].format(city=city)
        results  = tavily.search(query=query, search_depth="basic", max_results=3)
        raw_list = results.get("results", [])
        sources  = [{"title": r.get("title", ""), "url": r.get("url", "")}
                    for r in raw_list if r.get("url")]
        raw_weather = "\n".join([r.get("content", "") for r in raw_list if r.get("content")])

        if not raw_weather:
            raise ValueError("날씨 검색 결과 없음")

        chat_history = ChatHistory()
        chat_history.add_user_message(f"{config['weather_prompt']}\n\n{raw_weather}")
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )
        return str(response).strip(), sources

    except Exception as e:
        print(f"[web_search] 날씨 수집 실패: {e}")
        season = _get_season(now.month, config)
        return config["season_fallback"].get(season, ""), []


async def _get_barbershop_trend(tavily, kernel, config) -> tuple:
    """
    AG-001: 트렌드 수집 + JSON 파싱으로 안정성 강화.

    Returns:
        (trend_data, sources)
        trend_data = {
            "trend_summary": str,
            "target_analysis": str,
            "marketing_strategy": str
        }
    """
    fallback = {
        "trend_summary":      "페이드컷과 투블럭 스타일이 20-30대 남성 사이에서 인기",
        "target_analysis":    "깔끔한 이미지 원하는 직장인, 대학생",
        "marketing_strategy": "기술력 강조 + 예약 긴박감 CTA"
    }

    try:
        search_tasks = [
            asyncio.to_thread(
                tavily.search,
                query=query,
                search_depth="advanced",
                max_results=3
            )
            for query in config["trend_queries"]
        ]
        search_results = await asyncio.gather(*search_tasks)

        sources = [
            {"title": r.get("title", ""), "url": r.get("url", "")}
            for results in search_results
            for r in results.get("results", []) if r.get("url")
        ]
        raw_trend = "\n".join([
            r.get("content", "")
            for results in search_results
            for r in results.get("results", []) if r.get("content")
        ])

        if not raw_trend:
            raise ValueError("트렌드 검색 결과 없음")

        chat_history = ChatHistory()
        chat_history.add_user_message(config["trend_prompt"].format(raw_trend=raw_trend))
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        parsed = _parse_json_safe(str(response))

        trend_data = {
            "trend_summary":      parsed.get("trend_summary",      fallback["trend_summary"]),
            "target_analysis":    parsed.get("target_analysis",    fallback["target_analysis"]),
            "marketing_strategy": parsed.get("marketing_strategy", fallback["marketing_strategy"])
        }

        print(f"[web_search] 트렌드 분석 완료:")
        print(f"  요약  : {trend_data['trend_summary'][:50]}...")
        print(f"  타겟  : {trend_data['target_analysis']}")
        print(f"  전략  : {trend_data['marketing_strategy']}")

        return trend_data, sources

    except Exception as e:
        print(f"[web_search] 트렌드 수집 실패: {e} → fallback 사용")
        return fallback, []


async def _get_promo_info(kernel, now, config, trend_data: dict) -> str:
    """
    AG-002: promo 생성 시 trend_summary + target_analysis 반영.
    기존: 계절 정보만
    변경: 계절 + 트렌드 요약 + 타겟 고객 통합
    """
    try:
        today  = now.strftime("%Y년 %m월 %d일") if config["language"] == "Korean" \
                 else now.strftime("%B %d, %Y")
        season = _get_season(now.month, config)

        prompt = config["promo_prompt"].format(
            today           = today,
            season          = season,
            trend_summary   = trend_data.get("trend_summary", ""),
            target_analysis = trend_data.get("target_analysis", "")
        )

        chat_history = ChatHistory()
        chat_history.add_user_message(prompt)
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )
        return str(response).strip()

    except Exception as e:
        print(f"[web_search] 홍보포인트 생성 실패: {e}")
        season = _get_season(now.month, config)
        return config["season_fallback"].get(season, "")


def _get_season(month: int, config: dict) -> str:
    for months_tuple, season_name in config["seasons"].items():
        if month in months_tuple:
            return season_name
    return list(config["seasons"].values())[0]