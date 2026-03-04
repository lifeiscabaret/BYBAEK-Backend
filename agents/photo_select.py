import os
import json
from datetime import datetime, timezone, timedelta

from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory

# 동일 사진 재사용 금지 기간 
REUSE_COOLDOWN_DAYS = 14

# 최대 후보 수 
TOP_K_FOR_GPT = 10

# 점수 가중치 (합계 = 1.0)
WEIGHT_TREND      = 0.5   # 오늘 트렌드 키워드와의 일치도
WEIGHT_PREFERENCE = 0.3   # 사장님 온보딩에서 설정한 선호 스타일 일치도
WEIGHT_FRESHNESS  = 0.2   # 마지막 사용으로부터 경과 일수 (오래될수록 높음)


async def photo_select_agent(
    shop_id: str,
    trend_data: dict,
    photo_candidates: list,
    brand_settings: dict
) -> list:
    """
    오늘 게시물에 올릴 사진 선택 (메인 진입점)

    Args:
        shop_id:          샵 고유 ID (로깅용)
        trend_data:       web_search_agent 결과
                          {"trend": "페이드컷 인기...", "weather": "맑음 15도", "promo": "..."}
        photo_candidates: orchestrator가 get_top_photos()로 조회한 사진 리스트
                          # TODO: from services.cosmos_db import get_top_photos
                          #       photo_candidates = await get_top_photos(shop_id, limit=20)
        brand_settings:   온보딩 브랜드 설정
                          {
                            "photo_range":      {"min": 1, "max": 5},
                            "preferred_styles": ["fade_cut", "side_part"],
                            "upload_mood":      "깔끔하고 전문적인 느낌"
                          }

    Returns:
        선택된 사진 도큐먼트 리스트 (비어있으면 orchestrator가 날짜 범위 확장 후 재요청)
    """
    print(f"[photo_select] 시작 → shop_id={shop_id}, 후보={len(photo_candidates)}장")

    # 온보딩에서 설정한 업로드 사진 수 범위 읽기
    min_photos = brand_settings.get("photo_range", {}).get("min", 1)
    max_photos = brand_settings.get("photo_range", {}).get("max", 5)

    # 후보가 아예 없으면 즉시 반환 → orchestrator가 날짜 범위 넓혀서 재요청
    if not photo_candidates:
        print(f"[photo_select] 후보 없음 → 빈 리스트 반환 (orchestrator가 재요청 처리)")
        return []

    # STEP 1~3: 트렌드/선호도/신선도 점수 계산 + 14일 이내 사용 사진 제외
    scored = _score_photos(photo_candidates, trend_data, brand_settings)
    print(f"[photo_select] 점수 계산 완료 → "
          f"유효 후보 {len(scored)}장, 상위 {min(TOP_K_FOR_GPT, len(scored))}장 GPT 전달")

    # 점수 계산 후 유효 후보가 없으면 (전부 14일 이내 사용) 빈 리스트 반환
    if not scored:
        print(f"[photo_select] 유효 후보 없음 (전부 쿨다운 중) → 빈 리스트 반환")
        return []

    # STEP 4: 점수 상위 TOP_K장을 GPT에 전달해서 최종 선택
    kernel = _init_kernel()
    selected = await _gpt_select(
        kernel=kernel,
        scored_candidates=scored[:TOP_K_FOR_GPT],  # 비용 절감: 상위 10장만
        trend_data=trend_data,
        brand_settings=brand_settings,
        min_count=min_photos,
        max_count=max_photos
    )

    print(f"[photo_select] 완료 → {len(selected)}장 선택")
    return selected



# [점수 계산] 수학적 1차 필터링
def _score_photos(
    candidates: list,
    trend_data: dict,
    brand_settings: dict
) -> list:
    """
    각 사진의 오늘 게시물 적합도 점수 계산 후 내림차순 정렬

    점수 계산 방식:
      STEP 1 - 트렌드 일치도:
        오늘 트렌드 키워드 중 사진 style_tags에 포함된 비율
        트렌드 정보 없으면 0.5 (중간값)로 처리

      STEP 2 - 선호도 점수:
        사장님 온보딩 preferred_styles 중 사진 style_tags에 포함된 비율
        선호 스타일 미설정 시 fade_cut_score를 대신 사용

      STEP 3 - 신선도 점수:
        used_at 없음 (한 번도 안 쓴 사진) → 1.0 (최고점)
        used_at 있고 14일 미만 → 제외 (중복 방지)
        used_at 있고 14일 이상 → 경과일/30 (최대 1.0)

    최종점수 = trend*0.5 + preference*0.3 + freshness*0.2
    """
    trend_keywords   = _extract_trend_keywords(trend_data)
    preferred_styles = brand_settings.get("preferred_styles", [])

    # KST 기준 현재 시각 (타임존 인식 datetime)
    now_kst = datetime.now(timezone(timedelta(hours=9)))

    scored = []
    for photo in candidates:
        style_tags = photo.get("style_tags", [])

        # STEP 1: 트렌드 일치도
        if trend_keywords:
            matched = sum(1 for tag in style_tags if tag in trend_keywords)
            trend_score = min(1.0, matched / len(trend_keywords))
        else:
            trend_score = 0.5  # 중간값

        # STEP 2: 선호도 점수
        if preferred_styles:
            matched = sum(1 for tag in style_tags if tag in preferred_styles)
            preference_score = min(1.0, matched / len(preferred_styles))
        else:
            # 선호 스타일 미설정 시 페이드컷 선명도 점수 대체 사용
            preference_score = photo.get("fade_cut_score", 0.5)

        # STEP 3: 신선도 점수 + 14일 중복 방지
        used_at = photo.get("used_at")
        if used_at:
            # timezone 없는 ISO 문자열 안전하게 파싱 (Z → +00:00 변환)
            used_at_str = used_at.replace("Z", "+00:00")
            used_dt = datetime.fromisoformat(used_at_str)

            # tzinfo 없는 naive datetime이면 UTC로 간주
            if used_dt.tzinfo is None:
                used_dt = used_dt.replace(tzinfo=timezone.utc)

            days_ago = (now_kst - used_dt).days

            # 14일 이내 사용된 사진 → 중복 방지로 제외
            if days_ago < REUSE_COOLDOWN_DAYS:
                continue

            # 30일 기준으로 신선도 점수 계산 (30일 이상이면 1.0)
            freshness_score = min(1.0, days_ago / 30)
        else:
            # 한 번도 사용 안 한 사진 → 최고 신선도
            freshness_score = 1.0

        # 가중 합산
        final_score = (
            WEIGHT_TREND      * trend_score +
            WEIGHT_PREFERENCE * preference_score +
            WEIGHT_FRESHNESS  * freshness_score
        )

        scored.append({
            **photo,
            "_score": round(final_score, 4),
            "_score_detail": {   # 디버깅용 점수 세부내역
                "trend":      round(trend_score, 2),
                "preference": round(preference_score, 2),
                "freshness":  round(freshness_score, 2)
            }
        })

    # 점수 높은 순으로 정렬
    scored.sort(key=lambda x: x["_score"], reverse=True)
    return scored


# ──────────────────────────────────────────
# [트렌드 키워드 추출] 트렌드 텍스트 → style_tags 매핑
# ──────────────────────────────────────────

def _extract_trend_keywords(trend_data: dict) -> list:
    """
    web_search_agent가 반환한 트렌드 텍스트에서
    CosmosDB style_tags와 매핑 가능한 키워드 추출

    예) "페이드컷과 사이드파트 인기" → ["fade_cut", "side_part"]

    keyword_map에 새 스타일 추가 시 여기서만 수정하면 됨.
    한국어/영어 모두 커버 (글로벌 트렌드 서치 대응)
    """
    trend_text = trend_data.get("trend", "").lower()

    # style_tag → 매칭 키워드 (한국어 + 영어 포함)
    keyword_map = {
        "fade_cut":    ["페이드", "fade", "스킨페이드", "skin fade"],
        "side_part":   ["사이드파트", "side part", "7:3"],
        "slick_back":  ["슬릭백", "slick back", "slickback"],
        "two_block":   ["투블럭", "two block"],
        "ivy_league":  ["아이비리그", "ivy league"],
        "mullet":      ["멀릿", "mullet"],
        "buzz_cut":    ["버즈컷", "buzz cut", "crewcut", "크루컷"],
        "pompadour":   ["폼파두르", "pompadour"],
        "french_crop": ["프렌치크롭", "french crop", "크롭"],
        "textured":    ["텍스처", "texture", "레이어드", "layered"],
    }

    matched = [tag for tag, keywords in keyword_map.items()
               if any(kw in trend_text for kw in keywords)]

    print(f"[photo_select] 트렌드 키워드 추출: {matched}")
    return matched


# ──────────────────────────────────────────
# [GPT 최종 선택] 점수 상위 후보 → GPT가 최종 결정
# ──────────────────────────────────────────

async def _gpt_select(
    kernel: Kernel,
    scored_candidates: list,
    trend_data: dict,
    brand_settings: dict,
    min_count: int,
    max_count: int
) -> list:
    """
    점수 상위 후보 중 GPT-4.1-mini가 최종 선택

    수학 점수로 1차 필터링 후 GPT가 추가로 고려하는 것:
    - 오늘 날씨/분위기와 어울리는 사진
    - 비슷한 스타일끼리 중복 선택 방지 (다양성)
    - 사장님 업로드 무드 반영
    - 한 번도 안 쓴 사진 우선

    fallback: GPT 실패(파싱 오류 등) → 점수 순 자동 선택
    """
    # GPT에 넘길 사진 요약 (필요한 필드만 추려서 토큰 절감)
    candidates_summary = [
        {
            "id":             p["id"],
            "style_tags":     p.get("style_tags", []),
            "fade_cut_score": p.get("fade_cut_score", 0),
            "used_at":        p.get("used_at"),      # null이면 한 번도 안 쓴 사진
            "taken_at":       p.get("taken_at"),
            "score":          p.get("_score", 0),
            "score_detail":   p.get("_score_detail", {})
        }
        for p in scored_candidates
    ]

    prompt = f"""
너는 바버샵 인스타그램 마케터야. 오늘 게시물에 올릴 사진을 골라줘.

[오늘 트렌드]
{trend_data.get("trend", "정보 없음")}

[오늘 날씨]
{trend_data.get("weather", "정보 없음")}

[사장님 설정]
- 업로드 분위기: {brand_settings.get("upload_mood", "정보 없음")}
- 선호 스타일: {brand_settings.get("preferred_styles", [])}
- 금칙어: {brand_settings.get("forbidden_words", [])}

[사진 후보 (점수 높은 순)]
{json.dumps(candidates_summary, ensure_ascii=False, indent=2)}

[선택 규칙]
- 최소 {min_count}장, 최대 {max_count}장 선택
- 오늘 트렌드/날씨 분위기에 가장 잘 맞는 사진 우선
- 비슷한 스타일끼리 중복 선택 피하기 (다양한 조합 선호)
- used_at이 null인 사진 (한 번도 안 쓴 것) 우선 고려

반드시 아래 JSON 형식으로만 응답해. 다른 텍스트 절대 포함하지 마:
{{"selected_ids": ["photo_id_1", "photo_id_2"], "reason": "선택 이유 한 줄"}}
"""

    chat_history = ChatHistory()
    chat_history.add_user_message(prompt)

    try:
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        # JSON 파싱 (GPT가 마크다운 코드블록 감쌀 경우 대비해서 정제)
        raw = str(response).strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        selected_ids = result.get("selected_ids", [])
        reason       = result.get("reason", "")
        print(f"[photo_select] GPT 선택 완료 | 이유: {reason}")

        # selected_ids → 실제 사진 도큐먼트 매핑
        id_to_photo = {p["id"]: p for p in scored_candidates}
        selected = [id_to_photo[pid] for pid in selected_ids if pid in id_to_photo]

        # 범위 보정: min 미달이면 점수 순으로 보충
        if len(selected) < min_count:
            already = {p["id"] for p in selected}
            extra = [p for p in scored_candidates if p["id"] not in already]
            selected += extra[:min_count - len(selected)]
            print(f"[photo_select] min 미달 보충 → {len(selected)}장")

        # 범위 보정: max 초과이면 앞에서 자르기
        if len(selected) > max_count:
            selected = selected[:max_count]
            print(f"[photo_select] max 초과 자르기 → {len(selected)}장")

        return selected

    except Exception as e:
        # GPT 실패 시 점수 순 자동 fallback
        print(f"[photo_select] GPT 선택 실패 ({e}) → 점수 순 fallback")
        count = max(min_count, min(max_count, len(scored_candidates)))
        return scored_candidates[:count]


# ──────────────────────────────────────────
# [커널 초기화] mini 모델 전용
# ──────────────────────────────────────────

def _init_kernel() -> Kernel:
    """
    GPT-4.1-mini 기반 Semantic Kernel 초기화
    AZURE_OPENAI_DEPLOYMENT_MINI 없으면 AZURE_OPENAI_DEPLOYMENT로 fallback
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
# 지연님 함수 없이도 로직 검증 가능
# ──────────────────────────────────────────

if __name__ == "__main__":
    import asyncio

    # 목업 데이터 (CosmosDB get_top_photos 반환값 형태)
    # TODO: from services.cosmos_db import get_top_photos
    mock_photos = [
        {
            "id": "photo_001",
            "blob_url": "https://blob.../photo_001.jpg",
            "style_tags": ["fade_cut", "side_part"],
            "fade_cut_score": 0.92,
            "brightness": "good",
            "sharpness": "high",
            "is_usable": True,
            "used_at": None,   # 한 번도 안 쓴 사진
            "taken_at": "2026-02-01T10:00:00"
        },
        {
            "id": "photo_002",
            "blob_url": "https://blob.../photo_002.jpg",
            "style_tags": ["slick_back", "textured"],
            "fade_cut_score": 0.85,
            "brightness": "good",
            "sharpness": "high",
            "is_usable": True,
            "used_at": "2026-01-10T19:00:00",  # 50일 전 사용
            "taken_at": "2026-01-05T14:00:00"
        },
        {
            "id": "photo_003",
            "blob_url": "https://blob.../photo_003.jpg",
            "style_tags": ["two_block"],
            "fade_cut_score": 0.78,
            "brightness": "good",
            "sharpness": "medium",
            "is_usable": True,
            "used_at": "2026-02-28T19:00:00",  # 3일 전 → 쿨다운으로 제외되어야 함
            "taken_at": "2026-02-20T11:00:00"
        },
    ]

    mock_trend = {
        "trend": "2026년 봄 페이드컷(fade cut)과 사이드파트(side part) 인기 상승 중",
        "weather": "맑음 18도, 봄 시즌",
        "promo": "봄 신규 고객 이벤트 시즌"
    }

    mock_brand = {
        "photo_range":      {"min": 1, "max": 3},
        "preferred_styles": ["fade_cut", "side_part"],
        "upload_mood":      "깔끔하고 전문적인 바버샵 느낌",
        "forbidden_words":  ["저렴", "할인", "헤어샵"]
    }

    async def test():
        result = await photo_select_agent(
            shop_id="shop_test_001",
            trend_data=mock_trend,
            photo_candidates=mock_photos,
            brand_settings=mock_brand
        )
        print("\n[테스트 결과]")
        for p in result:
            print(f"  ✅ {p['id']} | 점수: {p.get('_score')} | 태그: {p.get('style_tags')}")
        print(f"\n총 {len(result)}장 선택")

    asyncio.run(test())