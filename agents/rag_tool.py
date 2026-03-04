"""
[지연님 연동 포인트]
  # TODO: from services.vector_db import save_embedding
  # TODO: from services.vector_db import search_similar_captions
  # TODO: from services.cosmos_db import get_recent_posts
"""

import os
import json
from datetime import datetime, timezone, timedelta
from openai import AsyncAzureOpenAI
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory


# Vector DB 검색 결과 수
TOP_K = 5

# 컨텍스트 압축 후 post_writer에 전달할 예시 수
MAX_EXAMPLES = 3

# 이 점수 이하 게시물은 후처리에서 제외 (부적합 예시 필터링)
# upload_status가 'cancel'인 게시물도 제외
MIN_QUALITY_SCORE = 0.0  # 현재는 cancel 여부로만 필터링

# [인덱싱] 업로드 완료 후 호출
async def index_post(
    shop_id: str,
    post_id: str,
    caption: str,
    hashtags: list,
    style_tags: list
) -> dict:
    """
    게시물 업로드 완료 후 Vector DB에 임베딩 저장 (인덱싱)

    orchestrator STEP 7에서 호출됨.
    누적될수록 RAG 검색 품질이 향상됨.

    Args:
        shop_id:    샵 ID (Vector DB 필터링용)
        post_id:    게시물 ID
        caption:    최종 업로드된 캡션
        hashtags:   사용된 해시태그 리스트
        style_tags: 사진에서 추출된 스타일 태그 리스트

    Returns:
        {"status": "indexed", "post_id": "...", "text_length": 123}
    """
    print(f"[rag_tool] 인덱싱 시작 → post_id={post_id}")

    # STEP 1: 텍스트 합치기 (캡션 + 해시태그 + 사진 스타일 태그)
    index_text = _build_index_text(caption, hashtags, style_tags)
    print(f"[rag_tool] 인덱스 텍스트: {index_text[:80]}...")

    # STEP 2: Azure OpenAI Embeddings 호출 → 벡터 변환
    embedding = await get_embedding(index_text)

    # STEP 3: Vector DB에 저장
    # TODO: from services.vector_db import save_embedding
    # await save_embedding(
    #     shop_id=shop_id,
    #     post_id=post_id,
    #     caption=caption,
    #     embedding=embedding
    # )

    # 목업
    print(f"[rag_tool] 인덱싱 완료 → shop_id={shop_id}, post_id={post_id}")
    return {
        "status": "indexed",
        "post_id": post_id,
        "text_length": len(index_text)
    }


def _build_index_text(caption: str, hashtags: list, style_tags: list) -> str:
    """
    인덱싱용 텍스트 생성
    예) "투블럭 스타일, 봄을 맞아 #바버샵 #투블럭 fade_cut side_part"
    """
    hashtag_str  = " ".join(hashtags)
    style_str    = " ".join(style_tags)
    return f"{caption} {hashtag_str} {style_str}".strip()

# [검색] orchestrator STEP 3에서 항상 호출
async def search_rag_context(
    shop_id: str,
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list = None
) -> dict:
    """
    RAG Tool 메인 함수 - 항상 호출, 데이터 없으면 Fallback 반환

    Args:
        shop_id:         샵 ID
        trend_data:      web_search_agent 결과
                         {"trend": "...", "weather": "...", "promo": "..."}
        selected_photos: photo_select_agent가 선택한 사진 메타 리스트
        brand_settings:  온보딩 브랜드 설정
        recent_posts:    최근 게시물 3개 (Fallback + 후처리용)
                         # TODO: from services.cosmos_db import get_recent_posts

    Returns:
        {
          "examples": [{"caption": "...", "hashtags": [...], "score": 0.92}, ...],
          "tone_rules": "친근하고 편안한 말투, 이모지 자주 사용...",
          "hashtag_patterns": ["#바버샵", "#페이드컷", ...],
          "cta_pattern": "DM으로 예약 문의주세요",
          "source": "vector_db" | "fallback"
        }
    """
    print(f"[rag_tool] RAG 검색 시작 → shop_id={shop_id}")

    if recent_posts is None:
        recent_posts = []

    # STEP 1: 검색 쿼리 생성 (브랜드 톤 + 트렌드 + 사진 특징 결합)
    query_text = _build_search_query(trend_data, selected_photos, brand_settings)
    print(f"[rag_tool] 검색 쿼리: {query_text[:80]}...")

    # STEP 2: 쿼리 → 벡터 변환
    query_vector = await get_embedding(query_text)

    # STEP 3: Vector DB 검색 시도
    # TODO: from services.vector_db import search_similar_captions
    # raw_results = await search_similar_captions(
    #     shop_id=shop_id,
    #     query_vector=query_vector,
    #     top_k=TOP_K
    # )

    # 목업: 지연님 함수 없이 단독 실행 가능 (빈 리스트 = Fallback으로 분기)
    raw_results = []

    # STEP 4: 결과 있으면 후처리, 없으면 Fallback
    if raw_results:
        print(f"[rag_tool] Vector DB 검색 결과 {len(raw_results)}개 → 후처리")

        # 후처리: 업로드된 글 우선 / 최신 우선 / 취소된 게시물 제외
        processed = _postprocess(raw_results)

        # 컨텍스트 압축: 톤 규칙 추출 + 좋은 예시 2~3개 + 해시태그 패턴
        context = await _compress_context(
            posts=processed,
            brand_settings=brand_settings
        )
        context["source"] = "vector_db"

    else:
        # Fallback: Vector DB 데이터 없을 때 (초기 or 데이터 부족)
        print(f"[rag_tool] Vector DB 데이터 없음 → Fallback 컨텍스트 생성")
        context = _build_fallback(recent_posts, brand_settings)
        context["source"] = "fallback"

    print(f"[rag_tool] RAG 완료 → source={context['source']}, "
          f"예시={len(context.get('examples', []))}개")
    return context


def _build_search_query(
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict
) -> str:
    """
    Vector DB 검색용 쿼리 텍스트 생성

    브랜드 톤 + 오늘 트렌드 + 선택된 사진 스타일 태그를 결합.
    임베딩 변환 후 유사한 과거 캡션 검색에 사용.

    예) "친근하고 편안한 말투 페이드컷 봄 트렌드 자연광 측면구도 fade_cut side_part"
    """
    parts = []

    # 브랜드 톤
    brand_tone = brand_settings.get("brand_tone", "")
    if brand_tone:
        parts.append(brand_tone)

    # 오늘 트렌드 요약
    trend = trend_data.get("trend", "")
    if trend:
        parts.append(trend[:100])  # 너무 길면 자르기

    # 선택된 사진들의 스타일 태그
    all_tags = []
    for photo in selected_photos:
        all_tags.extend(photo.get("style_tags", []))
    if all_tags:
        parts.append(" ".join(set(all_tags)))  # 중복 제거

    # 오늘 날씨/분위기
    weather = trend_data.get("weather", "")
    if weather:
        parts.append(weather)

    return " ".join(parts).strip()


def _postprocess(raw_results: list) -> list:
    """
    Vector DB 검색 결과 후처리

    1. 취소된 게시물 제외 (upload_status == 'cancel')
    2. 업로드 완료된 게시물 우선 (upload_status == 'success')
    3. 최신 게시물 우선 (uploaded_at 기준)
    """
    # 취소된 게시물 제외
    filtered = [
        p for p in raw_results
        if p.get("upload_status") != "cancel"
    ]

    # 업로드 완료 우선 정렬
    filtered.sort(key=lambda x: (
        0 if x.get("upload_status") == "success" else 1,  # success 먼저
        -(datetime.fromisoformat(
            x.get("uploaded_at", "2000-01-01").replace("Z", "+00:00")
        ).timestamp())  # 최신 먼저
    ))

    return filtered


async def _compress_context(posts: list, brand_settings: dict) -> dict:
    """
    후처리된 게시물에서 post_writer에 전달할 컨텍스트 압축

    GPT-4.1-mini를 사용해서:
    - 톤 규칙 추출 (말투, 이모지 사용 패턴)
    - 좋은 예시 2~3개 선별
    - 해시태그 패턴 정리
    - CTA 패턴 정리

    GPT 실패 시 단순 상위 MAX_EXAMPLES개로 fallback
    """
    if not posts:
        return _empty_context()

    kernel = _init_kernel()

    # GPT에 넘길 게시물 요약 (토큰 절감)
    posts_summary = [
        {
            "caption":       p.get("caption", ""),
            "hashtags":      p.get("hashtags", []),
            "cta":           p.get("cta", ""),
            "uploaded_at":   p.get("uploaded_at", ""),
            "upload_status": p.get("upload_status", "")
        }
        for p in posts[:10]  # 최대 10개만 GPT에 전달
    ]

    prompt = f"""
아래 바버샵 인스타그램 게시물들을 분석해서 글쓰기 가이드를 만들어줘.

[브랜드 설정]
- 톤: {brand_settings.get("brand_tone", "정보 없음")}
- 금칙어: {brand_settings.get("forbidden_words", [])}
- CTA: {brand_settings.get("cta", "정보 없음")}

[과거 게시물 샘플]
{json.dumps(posts_summary, ensure_ascii=False, indent=2)}

아래 JSON 형식으로만 응답해:
{{
  "tone_rules": "말투 특징 한 줄 요약 (예: 친근한 반말, 이모지 자주 사용)",
  "good_examples": [
    {{"caption": "...", "hashtags": [...], "why": "좋은 이유 한 줄"}}
  ],
  "hashtag_patterns": ["자주 쓰는 해시태그 리스트"],
  "cta_pattern": "자주 쓰는 CTA 문구"
}}
"""

    chat_history = ChatHistory()
    chat_history.add_user_message(prompt)

    try:
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        raw = str(response).strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        return {
            "examples":         result.get("good_examples", [])[:MAX_EXAMPLES],
            "tone_rules":       result.get("tone_rules", ""),
            "hashtag_patterns": result.get("hashtag_patterns", []),
            "cta_pattern":      result.get("cta_pattern", "")
        }

    except Exception as e:
        # GPT 실패 시 상위 MAX_EXAMPLES개 그대로 반환
        print(f"[rag_tool] 컨텍스트 압축 GPT 실패 ({e}) → 단순 fallback")
        return {
            "examples": [
                {"caption": p.get("caption", ""), "hashtags": p.get("hashtags", [])}
                for p in posts[:MAX_EXAMPLES]
            ],
            "tone_rules":       brand_settings.get("brand_tone", ""),
            "hashtag_patterns": [],
            "cta_pattern":      brand_settings.get("cta", "")
        }


def _build_fallback(recent_posts: list, brand_settings: dict) -> dict:
    """
    Vector DB 데이터 없을 때 Fallback 컨텍스트 생성

    최근 게시물 3개 + 브랜드 설정 요약으로 구성.
    온보딩 레퍼런스 사진의 style_tags도 포함하여 초기 품질 보장.
    """
    examples = [
        {
            "caption":  p.get("caption", ""),
            "hashtags": p.get("hashtags", [])
        }
        for p in (recent_posts or [])[:3]
    ]

    # 온보딩 레퍼런스 사진에서 추출된 선호 스타일도 힌트로 포함
    preferred_styles = brand_settings.get("preferred_styles", [])
    style_hint = f"선호 스타일: {', '.join(preferred_styles)}" if preferred_styles else ""

    tone_rules = brand_settings.get("brand_tone", "")
    if style_hint:
        tone_rules = f"{tone_rules}. {style_hint}".strip(". ")

    return {
        "examples":         examples,
        "tone_rules":       tone_rules,
        "hashtag_patterns": [],   # 초기엔 패턴 없음
        "cta_pattern":      brand_settings.get("cta", "")
    }


def _empty_context() -> dict:
    """데이터가 완전히 없을 때 빈 컨텍스트 반환 (구조 유지용)"""
    return {
        "examples":         [],
        "tone_rules":       "",
        "hashtag_patterns": [],
        "cta_pattern":      ""
    }


# ──────────────────────────────────────────
# [임베딩] Azure OpenAI Embeddings 호출
# ──────────────────────────────────────────

async def get_embedding(text: str) -> list:
    """
    텍스트 → 벡터 변환 (Azure OpenAI Embeddings)

    인덱싱과 검색 쿼리 모두 동일한 모델로 변환해야 유사도가 정확함.
    환경변수 AZURE_OPENAI_EMBEDDING_DEPLOYMENT 없으면 기본값 사용.

    Returns:
        float 리스트 (차원 수는 모델에 따라 다름, ada-002 기준 1536)
    """
    client = AsyncAzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_version="2024-02-01"
    )

    deployment = os.getenv(
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
        "text-embedding-ada-002"
    )

    try:
        response = await client.embeddings.create(
            model=deployment,
            input=text
        )
        return response.data[0].embedding

    except Exception as e:
        print(f"[rag_tool] 임베딩 실패 ({e}) → 빈 벡터 반환")
        return []   # 빈 벡터 반환 → Vector DB 검색 스킵 → Fallback으로 분기


# [커널 초기화] 컨텍스트 압축용 GPT
def _init_kernel() -> Kernel:
    """
    GPT-4.1-mini 기반 Semantic Kernel 초기화
    컨텍스트 압축(_compress_context)에서만 사용
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


# ──────────────────────────────────────────
# [목업 테스트] 단독 실행용
# 지연님 함수 없이도 전체 흐름 검증 가능
# ──────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()  
    import asyncio

    # 목업 데이터
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
        "preferred_styles": ["fade_cut", "side_part"]
    }

    # 최근 게시물 목업 (Fallback 테스트용)
    # TODO: from services.cosmos_db import get_recent_posts
    mock_recent_posts = [
        {
            "post_id": "post_001",
            "caption": "봄이 왔어요! 새로운 스타일로 변신해볼까요? ✂️",
            "hashtags": ["#바버샵", "#페이드컷", "#봄헤어"],
            "cta": "DM으로 예약 문의주세요",
            "upload_status": "success",
            "uploaded_at": "2026-02-20T19:00:00"
        }
    ]

    async def test():
        print("=" * 50)
        print("[테스트 1] 인덱싱")
        print("=" * 50)
        # 임베딩은 Azure 연결 필요 → 목업 환경에서는 에러 예상
        # 실제 테스트는 .env 설정 후 진행
        print("※ 임베딩 테스트는 Azure 연결 필요. Fallback 테스트 진행.")

        print("\n" + "=" * 50)
        print("[테스트 2] RAG 검색 (Fallback 경로)")
        print("=" * 50)

        # get_embedding이 빈 벡터 반환 → Fallback으로 분기되는지 확인
        result = await search_rag_context(
            shop_id="shop_test_001",
            trend_data=mock_trend,
            selected_photos=mock_photos,
            brand_settings=mock_brand,
            recent_posts=mock_recent_posts
        )

        print(f"\n[RAG 결과]")
        print(f"  source:           {result['source']}")
        print(f"  tone_rules:       {result['tone_rules']}")
        print(f"  cta_pattern:      {result['cta_pattern']}")
        print(f"  examples 수:      {len(result['examples'])}개")
        print(f"  hashtag_patterns: {result['hashtag_patterns']}")

    asyncio.run(test())