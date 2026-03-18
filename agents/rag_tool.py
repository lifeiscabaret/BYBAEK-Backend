import os
import json
from openai import AsyncAzureOpenAI
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory

# 설정값
TOP_K = 5
MAX_EXAMPLES = 3


# [임베딩] 텍스트 → 벡터 변환
async def get_embedding(text: str) -> list:
    """Azure OpenAI Embeddings로 텍스트를 벡터로 변환"""
    api_key = os.getenv("AZURE_OPENAI_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")

    if not api_key:
        print("[rag_tool] ❌ API Key 없음. .env 확인하세요.")
        return []

    client = AsyncAzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version="2024-02-01"
    )
    deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")

    try:
        response = await client.embeddings.create(model=deployment, input=text)
        return response.data[0].embedding
    except Exception as e:
        print(f"[rag_tool] ❌ 임베딩 생성 실패: {e}")
        return []


# [메인] orchestrator STEP 3에서 호출
async def search_rag_context(
    shop_id: str,
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list = None
) -> dict:
    """
    RAG 컨텍스트 검색 및 반환
    반환: { examples, tone_rules, hashtag_patterns, cta_pattern, source }
    """
    print(f"[rag_tool] RAG 검색 시작 → shop_id={shop_id}")

    if recent_posts is None:
        recent_posts = []

    # 1. 검색 쿼리 생성
    query_text = _build_search_query(trend_data, selected_photos, brand_settings)

    # 2. 쿼리 → 벡터
    query_vector = await get_embedding(query_text)
    if not query_vector:
        print("[rag_tool] 임베딩 실패 → fallback")
        return _build_fallback(recent_posts, brand_settings)

    # 3. Vector DB 검색
    from services.vector_db import search_similar_captions
    try:
        # 타입별 분리 검색
        body_results     = search_similar_captions(shop_id, query_vector, top_k=3,
                            query_text=query_text, content_type="caption_body")
        hashtag_results  = search_similar_captions(shop_id, query_vector, top_k=2,
                            query_text=query_text, content_type="hashtag_set")
        cta_results      = search_similar_captions(shop_id, query_vector, top_k=2,
                            query_text=query_text, content_type="cta")
        structure_results= search_similar_captions(shop_id, query_vector, top_k=1,
                            query_text=query_text, content_type="structure")

        # 타입 정보 포함해서 합치기
        raw_results = (
            [{"content_type": "caption_body",  **r} for r in body_results] +
            [{"content_type": "hashtag_set",   **r} for r in hashtag_results] +
            [{"content_type": "cta",           **r} for r in cta_results] +
            [{"content_type": "structure",     **r} for r in structure_results]
        )

        # 유사도 점수 로깅
        if raw_results:
            top_score = raw_results[0].get("@search.score", 0)
            print(f"[rag_tool] 타입별 검색 완료 → body:{len(body_results)} hashtag:{len(hashtag_results)} cta:{len(cta_results)} structure:{len(structure_results)}, top score={round(top_score, 4)}")
        else:
            print(f"[rag_tool] 검색 결과 없음")
    except Exception as e:
        print(f"[rag_tool] Vector DB 검색 에러: {e}")
        raw_results = []

    # 4. 결과 처리
    if raw_results:
        processed = _postprocess(raw_results)
        if processed:
            context = await _compress_context(processed, brand_settings)
            context["source"] = "vector_db"
            print(f"[rag_tool] ✅ Vector DB 검색 성공 → {len(processed)}개 결과 압축")
            return context

    # 5. 결과 없으면 fallback
    print("[rag_tool] Vector DB 결과 없음 → fallback")
    context = _build_fallback(recent_posts, brand_settings)
    context["source"] = "fallback"
    return context


# [헬퍼] 검색 쿼리 생성
def _build_search_query(trend_data: dict, selected_photos: list, brand_settings: dict) -> str:
    """브랜드톤 + 트렌드 + 사진 스타일 태그를 결합해 검색 쿼리 생성"""
    parts = []

    brand_tone = brand_settings.get("brand_tone", "")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)
    if brand_tone:
        parts.append(brand_tone)

    trend = trend_data.get("trend", "")
    if trend:
        parts.append(trend[:100])

    all_tags = []
    for photo in selected_photos:
        all_tags.extend(photo.get("style_tags", []))
    if all_tags:
        parts.append(" ".join(set(all_tags)))

    return " ".join(parts).strip()


# [헬퍼] 후처리
def _postprocess(raw_results: list) -> list:
    """
    Vector DB 결과는 id/caption만 있음
    → 필터링/정렬 없이 상위 MAX_EXAMPLES*2개만 반환
    """
    return raw_results[:MAX_EXAMPLES * 2]


# [헬퍼] GPT로 컨텍스트 압축 
async def _compress_context(posts: list, brand_settings: dict) -> dict:
    """
    검색된 과거 게시물을 GPT로 분석해
    tone_rules / 좋은 예시 2~3개 / hashtag_patterns / cta_pattern으로 압축
    """
    kernel = _init_kernel()
    chat = kernel.get_service("azure_openai")

    # 게시물 텍스트 정리
    # 타입별로 분리해서 GPT에 전달
    bodies     = [p for p in posts if p.get("content_type") == "caption_body"]
    hashtags   = [p for p in posts if p.get("content_type") == "hashtag_set"]
    ctas       = [p for p in posts if p.get("content_type") == "cta"]
    structures = [p for p in posts if p.get("content_type") == "structure"]

    body_text = "\n".join([f"- {p.get('caption','')}" for p in bodies[:3]])
    hashtag_text = "\n".join([f"- {p.get('caption','')}" for p in hashtags[:2]])
    cta_text = "\n".join([f"- {p.get('caption','')}" for p in ctas[:2]])
    structure_text = "\n".join([f"- {p.get('caption','')}" for p in structures[:1]])

    posts_text = f"""[본문 스타일 예시]
{body_text or "없음"}

[해시태그 조합 예시]
{hashtag_text or "없음"}

[CTA 문구 예시]
{cta_text or "없음"}

[글 구조 패턴]
{structure_text or "없음"}"""

    brand_tone = brand_settings.get("brand_tone", "")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)
    
    forbidden = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden, str):
        forbidden = [w.strip() for w in forbidden.split(",")]

    prompt = f"""다음은 한 바버샵의 과거 인스타그램 게시물들입니다.

[브랜드 톤]: {brand_tone}
[금칙어]: {', '.join(forbidden) if forbidden else '없음'}

[과거 게시물]:
{posts_text}

위 게시물들을 분석해서 아래 JSON 형식으로만 응답하세요. JSON 외 다른 텍스트 없이:
{{
  "tone_rules": "이 샵의 말투 특징을 2~3문장으로 요약",
  "examples": [
    {{"caption": "좋은 예시 캡션 1 (그대로 또는 요약)", "hashtags": ["#태그1", "#태그2"]}},
    {{"caption": "좋은 예시 캡션 2", "hashtags": ["#태그1", "#태그2"]}}
  ],
  "hashtag_patterns": ["자주 쓰는 해시태그 패턴 3~5개"],
  "cta_pattern": "자주 쓰는 CTA 문구"
}}"""

    history = ChatHistory()
    history.add_user_message(prompt)

    try:
        response = await chat.get_chat_message_content(
            chat_history=history,
            settings=chat.instantiate_prompt_execution_settings()
        )
        raw = str(response).strip()

        # JSON 파싱 (```json 블록 제거)
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)

        return {
            "examples": result.get("examples", [])[:MAX_EXAMPLES],
            "tone_rules": result.get("tone_rules", brand_tone),
            "hashtag_patterns": result.get("hashtag_patterns", []),
            "cta_pattern": result.get("cta_pattern", brand_settings.get("cta", ""))
        }

    except Exception as e:
        print(f"[rag_tool] GPT 압축 실패 ({e}) → fallback 구조 반환")
        return _build_fallback(posts, brand_settings)


# ─────────────────────────────────────────────
# [헬퍼] Fallback 컨텍스트 (Vector DB 데이터 없을 때)
# ─────────────────────────────────────────────
def _build_fallback(recent_posts: list, brand_settings: dict) -> dict:
    """Vector DB 데이터 없을 때 최근 게시물 + 브랜드 설정으로 fallback"""
    examples = [
        {"caption": p.get("caption", ""), "hashtags": p.get("hashtags", [])}
        for p in (recent_posts or [])[:MAX_EXAMPLES]
    ]
    
    brand_tone = brand_settings.get("brand_tone", "")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)
    
    return {
        "examples": examples,
        "tone_rules": brand_tone,
        "hashtag_patterns": [],
        "cta_pattern": brand_settings.get("cta", "")
    }


# ─────────────────────────────────────────────
# [헬퍼] Kernel 초기화
# ─────────────────────────────────────────────
def _init_kernel() -> Kernel:
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    api_key = os.getenv("AZURE_OPENAI_KEY") or os.getenv("AZURE_OPENAI_API_KEY")

    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=api_key
    ))
    return kernel