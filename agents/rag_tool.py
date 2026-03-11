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
        print("[rag_tool] API Key 없음. .env 확인하세요.")
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
        print(f"[rag_tool] 임베딩 생성 실패: {e}")
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
    반환: { examples, tone_rules, hashtag_patterns, cta_pattern, source, is_cold_start }

    Vector DB 결과 없을 때 rag_reference URL 크롤링 → 임베딩 저장 → 재검색
    검색 쿼리에 preferred_styles / stage2_tags 포함
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
        ctx = _build_fallback(recent_posts, brand_settings)
        ctx["source"] = "fallback"
        ctx["is_cold_start"] = False
        return ctx

    # 3. Vector DB 검색
    from services.vector_db import search_similar_captions
    try:
        raw_results = search_similar_captions(
            shop_id=shop_id,
            query_vector=query_vector,
            top_k=TOP_K
        )
    except Exception as e:
        print(f"[rag_tool] Vector DB 검색 에러: {e}")
        raw_results = []

    # 4. 결과 있으면 압축 후 반환
    if raw_results:
        processed = _postprocess(raw_results)
        if processed:
            context = await _compress_context(processed, brand_settings)
            context["source"] = "vector_db"
            context["is_cold_start"] = False
            print(f"[rag_tool] Vector DB 검색 성공 → {len(processed)}개 결과 압축")
            return context

    # 5. Vector DB 결과 없음 → cold start 처리
    print("[rag_tool] Vector DB 결과 없음 → cold start 시도")
    cold_start_result = await _handle_cold_start(
        shop_id=shop_id,
        brand_settings=brand_settings,
        query_vector=query_vector,
    )
    if cold_start_result:
        cold_start_result["is_cold_start"] = True
        return cold_start_result

    # 6. cold start도 실패하면 최종 fallback
    print("[rag_tool] cold start 실패 → 최종 fallback")
    ctx = _build_fallback(recent_posts, brand_settings)
    ctx["source"] = "fallback"
    ctx["is_cold_start"] = True
    return ctx


# cold start 핸들러
async def _handle_cold_start(
    shop_id: str,
    brand_settings: dict,
    query_vector: list,
) -> dict | None:
    """
    흐름:
    1. brand_settings에서 rag_reference URL 확인
    2. URL 크롤링해서 인스타 캡션 패턴 추출 (GPT)
    3. 각 캡션 임베딩 → Vector DB 저장
    4. 저장 직후 재검색 → 컨텍스트 반환

    반환: 압축된 RAG 컨텍스트 dict | None (실패 시)
    """
    rag_reference = brand_settings.get("rag_reference", "")
    if not rag_reference:
        print("[rag_tool] AG-030 rag_reference URL 없음 → cold start 스킵")
        return None

    print(f"[rag_tool] AG-030 cold start 시작 → {rag_reference}")

    # STEP 1-a: 사장님 기존 인스타 게시물 캡션 우선 수집
    captions = await _fetch_existing_captions(shop_id)

    # STEP 1-b: 기존 게시물 없으면 rag_reference + Tavily로 seed 생성
    if not captions:
        if not rag_reference:
            print("[rag_tool] AG-030 기존 게시물 없음 + rag_reference 없음 → cold start 불가")
            return None
        captions = await _generate_seed_captions(rag_reference, brand_settings)

    if not captions:
        print("[rag_tool] AG-030 캡션 수집/생성 실패")
        return None

    print(f"[rag_tool] AG-030 크롤링 완료 → {len(captions)}개 캡션 추출")

    # STEP 2: 캡션 임베딩 → Vector DB 저장
    from services.vector_db import save_embedding
    saved_count = 0
    for i, caption_text in enumerate(captions):
        emb = await get_embedding(caption_text)
        if not emb:
            continue
        post_id = f"cold_start_{shop_id}_{i:03d}"
        try:
            save_embedding(
                shop_id=shop_id,
                post_id=post_id,
                caption=caption_text,
                embedding=emb
            )
            saved_count += 1
        except Exception as e:
            print(f"[rag_tool] AG-030 임베딩 저장 실패 ({post_id}): {e}")

    print(f"[rag_tool] AG-030 임베딩 저장 완료 → {saved_count}개")

    if saved_count == 0:
        return None

    # STEP 3: 저장 직후 재검색
    from services.vector_db import search_similar_captions
    try:
        requery_results = search_similar_captions(
            shop_id=shop_id,
            query_vector=query_vector,
            top_k=TOP_K
        )
    except Exception as e:
        print(f"[rag_tool] AG-030 재검색 실패: {e}")
        return None

    if not requery_results:
        return None

    processed = _postprocess(requery_results)
    context = await _compress_context(processed, brand_settings)
    context["source"] = "cold_start"
    print(f"[rag_tool] AG-030 cold start 완료 → {len(processed)}개 컨텍스트 생성")
    return context


async def _fetch_existing_captions(shop_id: str) -> list[str]:
    """
    Instagram Graph API로 사장님 기존 게시물 캡션 가져오기.
    저장된 액세스 토큰으로 본인 계정 게시물을 공식 API로 조회.
    필요 권한: instagram_basic (온보딩 토큰에 포함 확인 필요)
    반환: 캡션 텍스트 리스트 (최대 10개, 빈 캡션 제외)
    """
    try:
        from services.cosmos_db import get_onboarding
        data = get_onboarding(shop_id)
        access_token = data.get("instagram_access_token", "") if data else ""

        if not access_token:
            print(f"[rag_tool] AG-030 인스타 액세스 토큰 없음 → 다음 단계로")
            return []

        import httpx
        url = "https://graph.instagram.com/me/media"
        params = {
            "fields": "caption,timestamp",
            "limit": 10,
            "access_token": access_token
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        captions = [
            item["caption"]
            for item in data.get("data", [])
            if item.get("caption", "").strip()
        ]

        print(f"[rag_tool] AG-030 인스타 기존 게시물 {len(captions)}개 캡션 수집 완료")
        return captions

    except Exception as e:
        print(f"[rag_tool] AG-030 인스타 캡션 수집 실패 ({e}) → 다음 단계로")
        return []


async def _generate_seed_captions(rag_reference: str, brand_settings: dict) -> list[str]:
    """
    AG-030 2순위 (fallback): rag_reference + Tavily 웹서치로 seed 캡션 생성.

    사장님 기존 게시물이 없을 때만 사용.
    Tavily로 레퍼런스 계정 관련 웹 정보 수집 → GPT로 캡션 5개 생성.
    """
    kernel = _init_kernel()
    chat = kernel.get_service("azure_openai")

    brand_tone = brand_settings.get("brand_tone", "친근한 말투")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)

    preferred_styles = brand_settings.get("preferred_styles", [])
    if isinstance(preferred_styles, str):
        preferred_styles = [s.strip() for s in preferred_styles.split(",") if s.strip()]
    preferred_str = ", ".join(preferred_styles) if preferred_styles else "페이드컷, 투블럭"

    cta = brand_settings.get("cta", "DM으로 예약 문의주세요")

    # Tavily로 레퍼런스 계정 웹 정보 검색
    web_context = ""
    try:
        from tavily import TavilyClient
        tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        account_name = rag_reference.rstrip("/").split("/")[-1].replace("@", "")
        query = f"{account_name} 바버샵 헤어스타일 인스타그램"
        results = tavily.search(query=query, search_depth="basic", max_results=3)
        snippets = [r.get("content", "") for r in results.get("results", []) if r.get("content")]
        web_context = "\n".join(snippets[:3])
        print(f"[rag_tool] AG-030 Tavily 검색 완료 → {len(snippets)}개 결과")
    except Exception as e:
        print(f"[rag_tool] AG-030 Tavily 검색 실패 ({e}) → 브랜드 설정만으로 생성")

    web_section = ("\n[레퍼런스 샵 웹 정보]\n" + web_context) if web_context else ""

    prompt = f"""바버샵 인스타그램 마케팅 전문가야.
아래 정보를 바탕으로 이 샵 스타일에 맞는 인스타그램 캡션 예시 5개를 만들어줘.
신규 계정의 RAG seed 데이터로 사용할 거야.

[레퍼런스 URL]: {rag_reference}{web_section}

[브랜드 설정]
- 말투: {brand_tone}
- 전문 스타일: {preferred_str}
- CTA: {cta}

[규칙]
- 실제 인스타그램에 올릴 수 있는 자연스러운 문장
- 첫 문장에 페이드컷 또는 전문 스타일명 포함
- 캡션마다 다른 상황/계절/분위기 (다양하게)
- 바버샵, 남성 헤어컷 범위 내에서만
- 긴박감 있는 CTA 포함

JSON으로만 응답:
{{"captions": ["캡션1", "캡션2", "캡션3", "캡션4", "캡션5"]}}"""

    history = ChatHistory()
    history.add_user_message(prompt)

    try:
        response = await chat.get_chat_message_content(
            chat_history=history,
            settings=chat.instantiate_prompt_execution_settings()
        )
        raw = str(response).strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        captions = result.get("captions", [])
        print(f"[rag_tool] AG-030 seed 캡션 {len(captions)}개 생성 완료")
        return captions
    except Exception as e:
        print(f"[rag_tool] AG-030 캡션 생성 실패: {e}")
        return []


# AG-041: 검색 쿼리 생성 개선
def _build_search_query(trend_data: dict, selected_photos: list, brand_settings: dict) -> str:
    """
    AG-041: 브랜드톤 + 트렌드 + 사진 태그 + preferred_styles 결합해 검색 쿼리 생성.
    더 많은 온보딩 필드를 반영할수록 Vector DB 검색 정확도 향상.
    """
    parts = []

    # 브랜드 톤
    brand_tone = brand_settings.get("brand_tone", "")
    if isinstance(brand_tone, list):
        brand_tone = " ".join(brand_tone)
    if brand_tone:
        parts.append(brand_tone)

    # 트렌드 요약
    trend = trend_data.get("trend", "")
    if trend:
        parts.append(trend[:100])

    # AG-041: preferred_styles 추가 (이 샵 전문 스타일)
    preferred_styles = brand_settings.get("preferred_styles", [])
    if isinstance(preferred_styles, str):
        preferred_styles = [s.strip() for s in preferred_styles.split(",") if s.strip()]
    if preferred_styles:
        parts.append(" ".join(preferred_styles))

    # 사진 태그 (stage2_tags 우선, style_tags fallback)
    all_tags = []
    for photo in selected_photos:
        tags = photo.get("stage2_tags") or photo.get("style_tags", [])
        all_tags.extend(tags)
    if all_tags:
        parts.append(" ".join(set(all_tags)))

    return " ".join(parts).strip()


# AG-041: 후처리 개선 (중복 캡션 제거)
def _postprocess(raw_results: list) -> list:
    """
    AG-041: Vector DB 결과에서 중복 캡션 제거 후 상위 MAX_EXAMPLES*2개 반환.
    동일/유사 캡션이 중복 저장된 경우 게시물 다양성 확보.
    """
    seen = set()
    deduped = []
    for item in raw_results:
        caption = item.get("caption", "").strip()
        # 앞 20자 기준 중복 제거
        key = caption[:20]
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:MAX_EXAMPLES * 2]


# [헬퍼] GPT로 컨텍스트 압축 
async def _compress_context(posts: list, brand_settings: dict) -> dict:
    """
    검색된 과거 게시물을 GPT로 분석해
    tone_rules / 좋은 예시 2~3개 / hashtag_patterns / cta_pattern으로 압축
    """
    kernel = _init_kernel()
    chat = kernel.get_service("azure_openai")

    # 게시물 텍스트 정리
    posts_text = "\n\n".join([
        f"[게시물 {i+1}]\n캡션: {p.get('caption', '')}"
        for i, p in enumerate(posts[:MAX_EXAMPLES * 2])
    ])

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

# [헬퍼] Fallback 컨텍스트 (Vector DB 데이터 없을 때)
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

# [헬퍼] Kernel 초기화
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