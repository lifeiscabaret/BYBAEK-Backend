import os
from openai import AsyncAzureOpenAI


async def rag_tool(
    shop_id: str,
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list
) -> dict:
    """
    RAG Tool (Agentic RAG)
    - 항상 호출 (데이터 없으면 Fallback)
    - Vector DB에서 shop_id 필터링 후 유사 캡션 TopK 검색
    - 결과를 게시물 작성 에이전트에 전달
    """

    # Step 1: 검색용 Query 생성
    query = _build_query(trend_data, selected_photos, brand_settings)

    # Step 2: Query 벡터 변환
    query_vector = await _get_embedding(query)

    # Step 3: Vector DB 검색 시도
    try:
        results = await _search_vector_db(shop_id, query_vector)

        if not results:
            # Fallback: 데이터 없으면 최근 게시물 + 브랜드 설정 사용
            return _fallback_context(recent_posts, brand_settings)

        # Step 4: 후처리 (부적합 예시 제외, 압축)
        refined = _postprocess(results)

        # Step 5: 패키징
        return _package_context(refined, brand_settings)

    except Exception:
        # Fallback
        return _fallback_context(recent_posts, brand_settings)


def _build_query(
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict
) -> str:
    """
    검색용 Query 생성
    브랜드 톤 + 트렌드 + 사진 특징 결합
    """
    tone = brand_settings.get("brand_tone", "")
    trend = trend_data.get("trend", "")
    weather = trend_data.get("weather", "")

    # TODO: 사진 태그 추가
    query = f"{tone} {trend} {weather}"
    return query.strip()


async def _get_embedding(text: str) -> list:
    """
    Azure OpenAI Embeddings로 텍스트 벡터 변환
    TODO: 실제 API 연동
    """
    client = AsyncAzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        api_version="2024-02-01",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
    )

    # TODO: 구현 예정
    # response = await client.embeddings.create(
    #     model="text-embedding-ada-002",
    #     input=text
    # )
    # return response.data[0].embedding

    return [0.1, 0.2, 0.3]  # 임시 벡터


async def _search_vector_db(shop_id: str, query_vector: list) -> list:
    """
    Vector DB에서 유사 캡션 TopK 검색
    TODO: Azure AI Search 연동 (지연님 함수 호출)
    """

    # TODO: 지연님 함수 호출
    # from services.vector_db import search_similar_captions
    # results = await search_similar_captions(shop_id, query_vector, top_k=5)

    return []  # 임시 빈 값


def _postprocess(results: list) -> list:
    """
    후처리
    - 업로드된 글 우선
    - 최신 글 우선
    - 부적합 예시 제외
    """

    # TODO: 정렬 및 필터링 로직 구현
    return results[:3]  # 최대 3개


def _package_context(results: list, brand_settings: dict) -> dict:
    """
    RAG 결과 패키징
    - 톤 규칙 추출
    - 좋은 예시 2~3개
    - 해시태그/CTA 패턴 정리
    """
    return {
        "examples": [r.get("caption") for r in results],
        "tone_rules": brand_settings.get("brand_tone", ""),
        "hashtag_pattern": brand_settings.get("hashtag_count", 10)
    }


def _fallback_context(recent_posts: list, brand_settings: dict) -> dict:
    """
    Fallback: Vector DB 데이터 없을 때
    최근 3개 게시물 + 브랜드 설정으로 대체
    """
    return {
        "examples": [p.get("caption") for p in recent_posts],
        "tone_rules": brand_settings.get("brand_tone", ""),
        "hashtag_pattern": brand_settings.get("hashtag_count", 10),
        "is_fallback": True
    }