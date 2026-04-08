"""
web_search.py - 웹 서치 에이전트 (파이프라인 시작점)

마케터 인터뷰 반영:
- "타겟팅 조사, 어디서 쓰이게 될지 파악"
- "메인 키워드를 첫 문단에"
- 성공 지표: 예약 문의 폭발

바버샵 특화:
- 고객 1순위: 페이드컷
- 타겟: 20-40대 남성 (직장인/대학생)
"""
import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from tavily import TavilyClient
from agents.competitor_analysis import competitor_analysis


def _get_trend_queries(year: int, month: int) -> list:
    """
    연도/월을 동적으로 반영한 트렌드 검색 쿼리 생성.
    커뮤니티/후기 위주로 실제 사람 표현 수집.
    """
    return [
        f"바버샵 페이드컷 후기 {year}",
        f"남자 헤어스타일 {year} 페이드컷 디시 커뮤니티",
        "남성 페이드컷 투블럭 인스타 후기 실제후기",
        "barbershop fade cut korea men review",
    ]



# 신뢰도 낮은 도메인 블랙리스트
BLOCKED_DOMAINS = [
    "ad.", "ads.", "sponsored", "promo",     # 광고
    "pinterest", "tiktok.com",               # 짧은 콘텐츠 플랫폼
    "aliexpress", "coupang", "gmarket",      # 쇼핑몰
]

# 바버샵 무관 / 유해 키워드 필터
IRRELEVANT_KEYWORDS = [
    "여성헤어", "여자헤어", "펌", "염색", "네일", "속눈썹",   # 여성 헤어
    "카지노", "도박", "성인", "불법",                          # 유해
    "다이어트", "헬스", "운동",                                # 무관
]

# 바버샵 관련 키워드 (이 중 하나라도 있어야 통과)
RELEVANT_KEYWORDS = [
    "바버", "barber", "페이드", "fade", "투블럭", "헤어컷",
    "남자머리", "남성헤어", "남자헤어", "사이드파트", "포마드",
    "크롭컷", "슬릭백", "리젠트", "언더컷"
]


def _filter_search_results(results: list) -> list:
    """
    검색 결과에서 신뢰도 낮은 소스, 무관 콘텐츠 제거.
    
    필터링 기준:
    1. 블랙리스트 도메인 차단
    2. 유해/무관 키워드 포함 시 제거
    3. 바버샵 관련 키워드 없으면 제거
    4. 내용이 너무 짧으면 제거 (100자 미만)
    
    Returns:
        필터링된 결과 리스트 (출처 URL 포함)
    """
    filtered = []
    blocked_count = 0
    irrelevant_count = 0

    for r in results:
        url     = r.get("url", "").lower()
        content = r.get("content", "")
        title   = r.get("title", "")
        text    = (content + " " + title).lower()

        # 1. 블랙리스트 도메인 차단
        if any(blocked in url for blocked in BLOCKED_DOMAINS):
            blocked_count += 1
            continue

        # 2. 유해/무관 키워드 필터
        if any(kw in text for kw in IRRELEVANT_KEYWORDS):
            irrelevant_count += 1
            continue

        # 3. 바버샵 관련성 체크
        if not any(kw in text for kw in RELEVANT_KEYWORDS):
            irrelevant_count += 1
            continue

        # 4. 너무 짧은 콘텐츠 제거
        if len(content) < 100:
            continue

        filtered.append(r)

    print(f"[web_search] 필터링 결과: {len(results)}개 → {len(filtered)}개 "
          f"(차단도메인:{blocked_count}, 무관:{irrelevant_count})")
    return filtered


def _extract_sources(results: list) -> list:
    """
    검색 결과에서 출처 정보 추출.
    도메인명도 함께 반환해서 신뢰도 파악 가능하게.
    """
    sources = []
    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        # 도메인 추출
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc.replace("www.", "")
        except Exception:
            domain = url

        sources.append({
            "title":  r.get("title", ""),
            "url":    url,
            "domain": domain
        })
    return sources

LOCALE_CONFIG = {
    "KR": {
        "language": "Korean",
        "timezone_offset": 9,
        "timezone_name": "KST",
        "weather_query": "{city} 오늘 날씨",
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
            "겨울": "연말 포마드컷, 리젠트로 스타일 변화 시즌"
        },
        "weather_prompt": (
            "아래 날씨 정보를 '맑음, 6도, 봄바람' 형식으로 한 줄 요약해줘. "
            "요약만 출력해."
        ),
        "trend_prompt": """아래는 바버샵 페이드컷 관련 실제 검색 결과야.

검색 결과:
{raw_trend}

아래 JSON 형식으로만 응답해줘. 설명/마크다운 없이 JSON만:
{{
  "trend_summary": "지금 어떤 스타일이 인기인지 2줄 이내. 스타일명 구체적으로.",
  "target_analysis": "어떤 고객층이 찾는지 1줄.",
  "marketing_strategy": "인스타 게시물에 쓸 수 있는 홍보 포인트 1줄. 긴박감 문구 금지.",
  "raw_snippets": [
    "검색 결과에서 뽑은 자연스러운 실제 표현 1 (사람이 직접 쓴 말투만)",
    "검색 결과에서 뽑은 자연스러운 실제 표현 2",
    "검색 결과에서 뽑은 자연스러운 실제 표현 3"
  ]
}}

raw_snippets 규칙:
- 검색 결과에 실제로 등장한 표현만 뽑을 것
- "~선사합니다", "~완성해드립니다" 같은 AI/광고 말투 금지
- 사람이 직접 후기 쓸 때 쓰는 자연스러운 말투만
- 없으면 빈 배열 []
- 바버샵/남성 헤어 무관한 내용 포함 금지
""",
        # 할루시네이션 방지
        "promo_prompt": (
            "오늘은 {today}이고 계절은 {season}이야.\n"
            "트렌드 요약: {trend_summary}\n"
            "타겟 고객: {target_analysis}\n\n"
            "위 정보를 바탕으로 바버샵 인스타그램 게시물에 쓸 수 있는 "
            "계절감 + 트렌드가 담긴 홍보 포인트를 1~2줄로 만들어줘.\n"
            "페이드컷 중심으로.\n"
            "확인되지 않은 사실(예약 현황, 자리 수, 마감 임박 등) 절대 포함 금지.\n"
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
    """GPT JSON 응답 안전 파싱. 마크다운 코드블록 제거 후 파싱."""
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
        "promo": "봄 트렌드 페이드컷 변화 적기",
        "raw_snippets": ["실제 표현1", "실제 표현2"],
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
    (weather, weather_sources), (trend_data, trend_sources), competitor_insights = await asyncio.gather(
        _get_weather(tavily, kernel, city, now, config),
        _get_barbershop_trend(tavily, kernel, config, now),
        competitor_analysis(shop_id, city)
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
        "raw_snippets":    trend_data.get("raw_snippets", []),  # ← post_writer 말투 참고용
        "locale":          locale,
        "city":            city,
        "competitor_insights": competitor_insights,
        "collected_at":    now.isoformat(),
        "sources_summary": [           # 출처 요약 (포트폴리오/디버깅용)
            f"{s['domain']}: {s['title'][:30]}"
            for s in (trend_sources or [])[:3]
        ]
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
        sources = _extract_sources(raw_list)
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


async def _get_barbershop_trend(tavily, kernel, config, now) -> tuple:
    """
    트렌드 수집 + JSON 파싱.
    연도/월 동적 반영, 커뮤니티/후기 위주 쿼리 사용.
    """
    fallback = {
        "trend_summary":      "페이드컷과 투블럭 스타일이 20-30대 남성 사이에서 인기",
        "target_analysis":    "깔끔한 이미지 원하는 직장인, 대학생",
        "marketing_strategy": "기술력 강조 + 자연스러운 예약 유도",
        "raw_snippets":       []
    }

    try:
        # [수정] 동적 쿼리 생성
        trend_queries = _get_trend_queries(now.year, now.month)

        search_tasks = [
            asyncio.to_thread(
                tavily.search,
                query=query,
                search_depth="advanced",
                max_results=3
            )
            for query in trend_queries
        ]
        search_results = await asyncio.gather(*search_tasks)

        all_results = [
            r
            for results in search_results
            for r in results.get("results", [])
        ]
        filtered_results = _filter_search_results(all_results)
        sources = _extract_sources(filtered_results)  


        raw_trend = "\n".join([
            r.get("content", "")
            for r in filtered_results if r.get("content")
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
            "marketing_strategy": parsed.get("marketing_strategy", fallback["marketing_strategy"]),
            "raw_snippets":       parsed.get("raw_snippets",       []),
            "sources":            sources
        }

        print(f"[web_search] 트렌드 분석 완료:")
        print(f"  요약  : {trend_data['trend_summary'][:50]}...")
        print(f"  타겟  : {trend_data['target_analysis']}")
        if trend_data["raw_snippets"]:
            print(f"  스니펫: {trend_data['raw_snippets'][0][:40]}...")

        return trend_data, sources

    except Exception as e:
        print(f"[web_search] 트렌드 수집 실패: {e} → fallback 사용")
        return fallback, []


async def _get_promo_info(kernel, now, config, trend_data: dict) -> str:
    """
    promo 생성. 할루시네이션 방지 강화.
    예약 현황, 자리 수, 마감 임박 등 확인 불가 정보 금지.
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