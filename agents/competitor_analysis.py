"""
web_search.py 하단에 추가할 경쟁샵 모니터링 코드
web_search_agent() 반환값에 competitor_insights 키가 추가됨
"""

import asyncio
from semantic_kernel.contents import ChatHistory

COMPETITOR_QUERIES = [
    "서울 바버샵 인스타그램 최근 게시물 페이드컷",
    "강남 홍대 바버샵 인스타 업로드 스타일",
    "barbershop korea instagram popular post fade",
]

async def competitor_analysis(shop_id: str, city: str = "서울") -> dict:
    from agents.web_search import _init_kernel, _init_tavily, _parse_json_safe  # ← 함수 안에서 import

    print(f"[competitor_analysis] 경쟁샵 분석 시작 → city={city}")

    tavily = _init_tavily()
    kernel = _init_kernel()

    try:
        search_tasks = [
            asyncio.to_thread(tavily.search, query=q, search_depth="basic", max_results=3)
            for q in COMPETITOR_QUERIES
        ]
        results = await asyncio.gather(*search_tasks, return_exceptions=True)

        all_snippets = []
        for r in results:
            if isinstance(r, Exception):
                continue
            for item in r.get("results", []):
                content = item.get("content", "")
                if content and len(content) > 80:
                    all_snippets.append(content[:300])

        if not all_snippets:
            return _competitor_fallback()

        raw_text = "\n---\n".join(all_snippets[:6])
        prompt = f"""아래는 서울 바버샵들의 최근 인스타그램 관련 검색 결과야.

[검색 결과]
{raw_text}

경쟁샵 분석 결과를 아래 JSON으로만 반환해줘:
{{
  "competitor_styles": ["경쟁샵이 주로 올리는 스타일 1", "스타일 2"],
  "competitor_hashtags": ["경쟁샵이 자주 쓰는 해시태그 1", "해시태그 2"],
  "gap_opportunity": "경쟁샵들이 놓치고 있는 틈새 타겟/스타일 1줄",
  "avoid_overlap": "이미 포화 상태라 차별화 어려운 것 1줄"
}}

JSON만 반환. 다른 텍스트 없이."""

        chat_history = ChatHistory()
        chat_history.add_user_message(prompt)
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        parsed = _parse_json_safe(str(response))
        if not parsed:
            return _competitor_fallback()

        result = {
            "competitor_styles":   parsed.get("competitor_styles", []),
            "competitor_hashtags": parsed.get("competitor_hashtags", []),
            "gap_opportunity":     parsed.get("gap_opportunity", ""),
            "avoid_overlap":       parsed.get("avoid_overlap", ""),
        }
        print(f"[competitor_analysis] 완료 → 틈새: {result['gap_opportunity'][:40]}")
        return result

    except Exception as e:
        print(f"[competitor_analysis] 실패 ({e}) → fallback")
        return _competitor_fallback()


def _competitor_fallback() -> dict:
    return {
        "competitor_styles":   ["페이드컷", "투블럭"],
        "competitor_hashtags": ["#바버샵", "#페이드컷", "#남자헤어"],
        "gap_opportunity":     "직장인 출근룩 연계 콘텐츠 부족",
        "avoid_overlap":       "일반 페이드컷 사진은 이미 포화",
    }

