# gpt-4.1-mini + Tavily 버전
import os
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
            "2026 바버샵 남성 헤어스타일 페이드컷 슬릭백 사이드파트 트렌드 추천 한국",
            "2026 barbershop men haircut fade slickback sidepart pompadour ivy league trend Korea"
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
            "아래 날씨 정보를 '맑음, 6도, 봄바람' 형식으로 한 줄 요약해줘. "
            "요약만 출력해."
        ),
        "trend_prompt": (
            "아래 내용에서 바버샵 전용 남성 헤어스타일 트렌드만 2~3줄로 요약해줘. "
            "반드시 아래 바버샵 스타일 중에서만 언급해줘: "
            "페이드컷, 사이드파트, 슬릭백, 아이비리그컷, 포마드컷, "
            "가일컷, 버스트페이드, 크루컷, 크롭컷, 드랍컷, "
            "플랫탑, 롱트림, 리젠트컷, 버즈컷, 슬릭백언더컷, 멀릿컷, 모히칸. "
            "여성 헤어, 펌, 염색, 미용실 관련 내용은 절대 포함하지 마. "
            "요약만 출력해."
        ),
        "promo_prompt": (
            "오늘은 {today}이고 계절은 {season}이야. "
            "바버샵 인스타그램 게시물에 쓸 수 있는 "
            "계절감 있는 홍보 포인트를 1~2줄로 만들어줘. "
            "페이드컷, 사이드파트, 슬릭백 같은 바버샵 스타일 이름을 자연스럽게 포함해줘. "
            "홍보 포인트만 출력해."
        )
    },
    "EN": {
        "language": "English",
        "timezone_offset": 0,
        "timezone_name": "UTC",
        "weather_query": "today weather in {city}",
        "trend_queries": [
            "2026 barbershop fade cut sidepart slickback ivy league pompadour trend",
            "2026 men barbershop haircut burst fade crew cut drop fade trend"
        ],
        "seasons": {
            (3, 4, 5): "Spring",
            (6, 7, 8): "Summer",
            (9, 10, 11): "Fall",
            (12, 1, 2): "Winter"
        },
        "season_fallback": {
            "Spring": "Spring season - fresh fade cut or sidepart for a clean new look",
            "Summer": "Summer vibes - skin fade and crew cut season is here",
            "Fall": "Fall season - slickback and ivy league cut are trending",
            "Winter": "Year-end special - pompadour and regent cut to stand out"
        },
        "weather_prompt": (
            "Summarize the weather info below in one line like 'Sunny, 72°F, breezy'. "
            "Output the summary only."
        ),
        "trend_prompt": (
            "Summarize the latest barbershop trends from the content below in 2-3 lines. "
            "Only mention barbershop-specific men's styles from this list: "
            "fade cut, sidepart, slickback, ivy league, pompadour, "
            "burst fade, crew cut, crop cut, drop fade, flat top, "
            "long trim, regent cut, buzz cut, slickback undercut, mullet, mohawk. "
            "Do NOT include women's hair, perm, coloring, or salon content. "
            "Output the summary only."
        ),
        "promo_prompt": (
            "Today is {today} and the season is {season}. "
            "Create a 1-2 line seasonal promotion for a barbershop Instagram post. "
            "Naturally include barbershop style names like fade, sidepart, or slickback. "
            "Output the promotion point only."
        )
    }
}


def _init_kernel() -> Kernel:
    """
    Semantic Kernel 초기화
    [변경] DEPLOYMENT_MINI 우선 사용, 없으면 DEPLOYMENT fallback
    web_search는 요약 작업이라 mini로 충분
    """
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


# [목업] Cosmos DB 함수 연동 전 임시 함수
async def _get_shop_info_mock(shop_id: str) -> dict:
    # TODO: from services.cosmos_db import get_shop_location
    # ⚠️ 현재 "서울" 고정 → get_shop_location(shop_id) 연동 시 사장님 실제 위치로 교체
    # 반환 예시: {"city": "부산", "locale": "KR", "timezone_offset": 9}
    return {"city": "서울", "locale": "KR", "timezone_offset": 9}


async def _get_cache_mock(shop_id: str, date_str: str) -> dict | None:
    # TODO: from services.cosmos_db import get_today_web_search_cache
    return None


async def _save_cache_mock(shop_id: str, result: dict) -> None:
    # TODO: from services.cosmos_db import save_web_search_cache
    pass


async def web_search_agent(shop_id: str, force_refresh: bool = False) -> dict:
    """
    웹 서치 에이전트 메인 함수
    orchestrator STEP 1 병렬 수집에서 호출됨.

    Args:
        shop_id:       샵 고유 ID
        force_refresh: True면 캐시 무시하고 재검색 (Self-Eval 재시도 시)

    Returns:
        {
            "weather": "맑음, 6도, 봄바람",
            "weather_sources": [...],
            "trend": "페이드컷, 사이드파트 인기...",
            "trend_sources": [...],
            "promo": "봄 시즌 헤어스타일 변화 적기",
            "locale": "KR",
            "city": "서울",
            "collected_at": "2026-03-01T20:00:00+09:00"
        }
    """
    shop_info = await _get_shop_info_mock(shop_id)
    locale = shop_info.get("locale", "KR")
    city = shop_info.get("city", "서울")
    config = LOCALE_CONFIG[locale]

    tz_offset = timedelta(hours=config["timezone_offset"])
    local_tz = timezone(tz_offset)
    now = datetime.now(local_tz)
    today_str = now.strftime("%Y-%m-%d")

    if not force_refresh:
        cached = await _get_cache_mock(shop_id, today_str)
        if cached:
            print(f"[web_search] 캐시 히트 → shop_id={shop_id}, date={today_str}")
            return cached
    else:
        print(f"[web_search] force_refresh=True → 캐시 무시하고 재검색")

    kernel = _init_kernel()
    tavily = _init_tavily()

    (weather, weather_sources), (trend, trend_sources), promo = await asyncio.gather(
        _get_weather(tavily, kernel, city, now, config),
        _get_barbershop_trend(tavily, kernel, config),
        _get_promo_info(kernel, now, config)
    )

    result = {
        "weather": weather,
        "weather_sources": weather_sources,
        "trend": trend,
        "trend_sources": trend_sources,
        "promo": promo,
        "locale": locale,
        "city": city,
        "collected_at": now.isoformat()
    }

    await _save_cache_mock(shop_id, result)
    return result


async def _get_weather(tavily, kernel, city, now, config) -> tuple:
    try:
        query = config["weather_query"].format(city=city)
        results = tavily.search(query=query, search_depth="basic", max_results=3)
        raw_list = results.get("results", [])
        sources = [{"title": r.get("title", ""), "url": r.get("url", "")}
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
    try:
        search_tasks = [
            asyncio.to_thread(tavily.search, query=query,
                              search_depth="advanced", max_results=3)
            for query in config["trend_queries"]
        ]
        search_results = await asyncio.gather(*search_tasks)
        sources = [{"title": r.get("title", ""), "url": r.get("url", "")}
                   for results in search_results
                   for r in results.get("results", []) if r.get("url")]
        raw_trend = "\n".join([r.get("content", "")
                               for results in search_results
                               for r in results.get("results", []) if r.get("content")])
        if not raw_trend:
            raise ValueError("트렌드 검색 결과 없음")
        chat_history = ChatHistory()
        chat_history.add_user_message(f"{config['trend_prompt']}\n\n{raw_trend}")
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )
        return str(response).strip(), sources
    except Exception as e:
        print(f"[web_search] 트렌드 수집 실패: {e}")
        if config["language"] == "Korean":
            return "투블럭, 레이어드컷, 클래식 바버샵 스타일 인기", []
        else:
            return "Fade cuts, taper cuts, and classic barbershop styles are trending", []


async def _get_promo_info(kernel, now, config) -> str:
    try:
        today = now.strftime("%Y년 %m월 %d일") if config["language"] == "Korean" \
            else now.strftime("%B %d, %Y")
        season = _get_season(now.month, config)
        prompt = config["promo_prompt"].format(today=today, season=season)
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