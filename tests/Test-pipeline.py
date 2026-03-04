import asyncio
import json
from dotenv import load_dotenv
load_dotenv()

from agents.photo_filter import run_stage2_filter
from agents.photo_select import photo_select_agent
from agents.rag_tool import search_rag_context
from agents.post_writer import post_writer_agent

# [테스트 데이터] 실제 Blob URL 사용
BLOB_BASE = "https://stctrla.blob.core.windows.net/photos"

STAGE1_PASS_LIST = [
    {
        "image_id":      "01_1_guileCut",
        "blob_url":      f"{BLOB_BASE}/01_1_guileCut.png",
        "stage1_score":  0.85,
        "stage1_reason": "pass",
        "metadata":      {"taken_at": "2026-03-01", "resolution": "1080x1080"}
    },
    {
        "image_id":      "01_2_guileCut",
        "blob_url":      f"{BLOB_BASE}/01_2_guileCut.jpeg",
        "stage1_score":  0.82,
        "stage1_reason": "pass",
        "metadata":      {"taken_at": "2026-03-01", "resolution": "1080x1350"}
    },
    {
        "image_id":      "02_1_burstFadeCut",
        "blob_url":      f"{BLOB_BASE}/02_1_burstFadeCut.jpg",
        "stage1_score":  0.90,
        "stage1_reason": "pass",
        "metadata":      {"taken_at": "2026-03-02", "resolution": "1080x1080"}
    },
    {
        "image_id":      "03_1_sidePart",
        "blob_url":      f"{BLOB_BASE}/03_1_sidePart.jpg",
        "stage1_score":  0.78,
        "stage1_reason": "pass",
        "metadata":      {"taken_at": "2026-03-02", "resolution": "1080x1350"}
    },
    {
        "image_id":      "04_1_crewCut",
        "blob_url":      f"{BLOB_BASE}/04_1_crewCut.png",
        "stage1_score":  0.88,
        "stage1_reason": "pass",
        "metadata":      {"taken_at": "2026-03-03", "resolution": "1080x1080"}
    },
]

# 브랜드 설정 목업
MOCK_BRAND = {
    "brand_tone": "친근하고 편안한 말투",
    "forbidden_words": ["저렴", "할인"],
    "cta": "DM으로 예약 문의주세요",
    "preferred_styles": ["fade_cut", "side_part"],
    "feed_style": {
        "emoji_usage": "자주",
        "caption_length": "2~4줄",
        "hashtag_count": 10
    }
}

# 트렌드 목업 (web_search_agent 결과 형태)
MOCK_TREND = {
    "trend": "2026년 봄 페이드컷과 사이드파트 인기 상승 중. 자연스러운 텍스처와 클린한 라인이 트렌드.",
    "weather": "맑음 18도, 완연한 봄 날씨",
    "promo": "봄 시즌 신규 고객 이벤트 진행 중인 바버샵 증가"
}

# 최근 게시물 목업 (RAG Fallback용)
MOCK_RECENT_POSTS = [
    {
        "post_id": "post_001",
        "caption": "새봄, 새스타일! 페이드컷으로 산뜻하게 시작해요 🌿✂️",
        "hashtags": ["#바버샵", "#페이드컷", "#봄헤어"],
        "cta": "DM으로 예약 문의주세요",
        "upload_status": "success",
        "uploaded_at": "2026-02-20T19:00:00"
    },
    {
        "post_id": "post_002",
        "caption": "깔끔한 사이드파트로 오늘도 멋지게 ✨",
        "hashtags": ["#바버샵", "#사이드파트", "#남성헤어"],
        "cta": "DM으로 예약 문의주세요",
        "upload_status": "success",
        "uploaded_at": "2026-02-15T19:00:00"
    }
]


# [테스트 실행]
async def run_test():
    print("\n" + "=" * 60)
    print("  BYBAEK 파이프라인 실제 테스트")
    print("=" * 60)

    shop_id = "shop_test_001"
    results = {}

    # STEP 1. photo_filter
    print("\n📸 STEP 1. photo_filter (2차 필터링)")
    print("-" * 40)

    filter_result = await run_stage2_filter(
        shop_id=shop_id,
        stage1_pass_list=STAGE1_PASS_LIST
    )

    print(f"\n[필터링 결과]")
    print(f"  전체: {filter_result['total']}장")
    print(f"  PASS: {filter_result['passed']}장")
    print(f"  FAIL: {filter_result['failed']}장")

    # PASS된 사진만 다음 단계로
    passed_photos = [r for r in filter_result["results"] if r.get("stage2_pass")]

    if not passed_photos:
        print("  ⚠️ 통과한 사진 없음 → 전체 사진 그대로 사용 (fallback)")
        # Fallback: 필터링 실패 시 원본 목업 형태로 변환
        passed_photos = [
            {
                "id": p["image_id"],
                "photo_id": p["image_id"],
                "blob_url": p["blob_url"],
                "style_tags": [],
                "fade_cut_score": p["stage1_score"],
                "is_usable": True,
                "used_at": None
            }
            for p in STAGE1_PASS_LIST
        ]
    else:
        # photo_select가 읽는 형태로 변환
        passed_photos = [
            {
                "id": r["image_id"],
                "photo_id": r["image_id"],
                "blob_url": next(
                    (p["blob_url"] for p in STAGE1_PASS_LIST if p["image_id"] == r["image_id"]),
                    ""
                ),
                "style_tags": r.get("stage2_tags", []),
                "fade_cut_score": r.get("fade_cut_score", 0.5),
                "is_usable": True,
                "used_at": None
            }
            for r in passed_photos
        ]

    results["filter"] = filter_result
    print(f"\n  → 다음 단계로 넘길 사진: {len(passed_photos)}장")
    for p in passed_photos:
        print(f"    - {p['id']} | 태그: {p['style_tags']}")

    # STEP 2. photo_select
    print("\n\n🖼️  STEP 2. photo_select (사진 선택)")
    print("-" * 40)

    selected_photos = await photo_select_agent(
        shop_id=shop_id,
        trend_data=MOCK_TREND,
        photo_candidates=passed_photos,
        brand_settings=MOCK_BRAND
    )

    print(f"\n[선택 결과]")
    print(f"  선택된 사진: {len(selected_photos)}장")
    for p in selected_photos:
        print(f"  - {p.get('id', p.get('photo_id'))} | 태그: {p.get('style_tags', [])}")

    results["selected_photos"] = selected_photos

    # STEP 3. rag_tool
    print("\n\n🔍 STEP 3. rag_tool (RAG 컨텍스트)")
    print("-" * 40)

    rag_context = await search_rag_context(
        shop_id=shop_id,
        trend_data=MOCK_TREND,
        selected_photos=selected_photos,
        brand_settings=MOCK_BRAND,
        recent_posts=MOCK_RECENT_POSTS
    )

    print(f"\n[RAG 결과]")
    print(f"  source:     {rag_context['source']}")
    print(f"  tone_rules: {rag_context['tone_rules']}")
    print(f"  예시 수:    {len(rag_context['examples'])}개")

    results["rag"] = rag_context

    # STEP 4. post_writer
    print("\n\n✍️  STEP 4. post_writer (게시물 작성)")
    print("-" * 40)

    post_draft = await post_writer_agent(
        shop_id=shop_id,
        trend_data=MOCK_TREND,
        selected_photos=selected_photos,
        brand_settings=MOCK_BRAND,
        recent_posts=MOCK_RECENT_POSTS,
        rag_context=rag_context
    )

    results["post"] = post_draft
    
    # 최종 결과 출력
    print("\n\n" + "=" * 60)
    print("  ✅ 최종 결과")
    print("=" * 60)
    print(f"\n📝 캡션:\n{post_draft['caption']}")
    print(f"\n🏷️  해시태그:\n{' '.join(post_draft['hashtags'])}")
    print(f"\n📣 CTA:\n{post_draft['cta']}")
    print(f"\n📸 사용 사진 ({len(selected_photos)}장):")
    for p in selected_photos:
        print(f"  - {p.get('blob_url', '')}")

    print("\n" + "=" * 60)
    print("  테스트 완료!")
    print("=" * 60)

    return results


if __name__ == "__main__":
    asyncio.run(run_test())