import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

from services.cosmos_client import get_cosmos_container
from agents.web_search import web_search_agent
from agents.photo_filter import run_stage2_filter
from agents.photo_select import photo_select_agent
from agents.rag_tool import search_rag_context
from agents.post_writer import post_writer_agent

# ✅ 실제 DB의 shop_id로 변경
SHOP_ID = "3sesac18"

# ✅ 실제 Photo 컨테이너의 id + blob_url로 변경
STAGE1_PASS_LIST = [
    {
        "image_id": "photo_3sesac18_01_1_guileCut",
        "blob_url": "https://stctrla.blob.core.windows.net/photos/01_1_guileCut.png",
        "stage1_score": 0.85
    },
    {
        "image_id": "photo_3sesac18_02_1_burstFadeCut",
        "blob_url": "https://stctrla.blob.core.windows.net/photos/02_1_burstFadeCut.jpg",
        "stage1_score": 0.90
    },
    {
        "image_id": "photo_3sesac18_15_1_fadeCut",
        "blob_url": "https://stctrla.blob.core.windows.net/photos/15_1_fadeCut.png",
        "stage1_score": 0.78
    },
]

# 최근 게시물 목업 (RAG Fallback용 - 실제 Post 데이터 없을 때)
MOCK_RECENT_POSTS = [
    {
        "post_id": "post_001",
        "caption": "새봄, 새스타일! 페이드컷으로 산뜻하게 시작해요 🌿✂️",
        "hashtags": ["#바버샵", "#페이드컷", "#봄헤어"],
        "cta": "DM으로 예약 문의주세요",
        "upload_status": "success",
        "uploaded_at": "2026-02-20T19:00:00"
    }
]


async def run_test():
    print("\n" + "=" * 60)
    print("  BYBAEK 파이프라인 [실시간 웹 + 실데이터 DB 통합 테스트]")
    print("=" * 60)

    # ── STEP 0-1. 웹 서치 (force_refresh=True로 캐시 무시) ──────
    print(f"\n🌐 STEP 0-1. web_search_agent (실시간 검색 중...)")
    try:
        real_trend = await web_search_agent(SHOP_ID, force_refresh=True)  # ✅ 캐시 무시!
        print(f"✅ 트렌드 검색 완료! (날씨: {real_trend.get('weather')})")
    except Exception as e:
        print(f"❌ 트렌드 검색 실패: {e}")
        return

   # ── STEP 0-2. 브랜드 설정 로드 ────────────────────────────
    print(f"\n🔍 DB(Shop 컨테이너)에서 '{SHOP_ID}' 설정을 로드합니다...")
    container = get_cosmos_container("Shop")

    def to_list(val):
        if isinstance(val, list): return val
        if isinstance(val, str) and val: return [v.strip() for v in val.split(",")]
        return []
    
    def to_string(val):
        """list를 string으로 변환"""
        if isinstance(val, list):
            return ", ".join(val)
        return str(val) if val else ""

    try:
        query = f"SELECT * FROM c WHERE c.id = '{SHOP_ID}'"
        items = list(container.query_items(query=query, enable_cross_partition_query=True))

        if not items:
            print(f"⚠️  Shop DB에 '{SHOP_ID}' 없음 → 기본값으로 진행")
            brand_settings = {
                "brand_tone": "친근하고 편안한 말투",
                "forbidden_words": ["저렴", "할인"],
                "cta": "DM으로 예약 문의주세요",
                "preferred_styles": ["fade_cut", "side_part"],
                "feed_style": {"emoji_usage": "자주", "caption_length": "2~4줄", "hashtag_count": 10}
            }
        else:
            db_brand = items[0]
            brand_settings = {
                "brand_tone": to_string(db_brand.get("brand_tone", "친근하고 편안한 말투")),  # ✅ string 변환
                "forbidden_words": to_list(db_brand.get("forbidden_words")),
                "cta": db_brand.get("cta", "DM으로 예약 문의주세요"),
                "preferred_styles": to_list(db_brand.get("preferred_styles")),
                "feed_style": db_brand.get("feed_style", {}),
                "brand_differentiation": db_brand.get("shop_intro", "")
            }
            print(f"✅ DB 설정 로드 성공! (말투: {brand_settings['brand_tone']})")

    except Exception as e:
        print(f"❌ DB 로드 중 에러 발생: {e}")
        return

    # ── STEP 1. photo_filter ───────────────────────────────────
    print("\n📸 STEP 1. photo_filter (비전 분석 시작)")
    filter_result = await run_stage2_filter(
        shop_id=SHOP_ID,
        stage1_pass_list=STAGE1_PASS_LIST
    )

    passed_photos = [
        {
            "id": r["image_id"],
            "photo_id": r["image_id"],
            "style_tags": r.get("stage2_tags", []),
            "blob_url": next(
                p["blob_url"] for p in STAGE1_PASS_LIST
                if p["image_id"] == r["image_id"]
            ),
            "fade_cut_score": r.get("fade_cut_score", 0.5),
            "detected_angle": r.get("detected_angle", "unknown"),  # ✅ 추가!
            "is_usable": True
        }
        for r in filter_result["results"] if r.get("stage2_pass")
    ]

    # 통과 사진 없으면 전체로 강제 진행
    if not passed_photos:
        print("⚠️  PASS 사진 없음 → 전체 사진으로 강제 진행")
        passed_photos = [
            {
                "id": p["image_id"],
                "photo_id": p["image_id"],
                "blob_url": p["blob_url"],
                "style_tags": [],
                "detected_angle": "unknown",  # ✅ 추가!
                "is_usable": True
            }
            for p in STAGE1_PASS_LIST
        ]

    # ── STEP 2. photo_select ───────────────────────────────────
    print("\n🖼️  STEP 2. photo_select (실시간 트렌드 반영)")
    selected_photos = await photo_select_agent(
        shop_id=SHOP_ID,
        trend_data=real_trend,
        photo_candidates=passed_photos,
        brand_settings=brand_settings
    )

    # ── STEP 3. rag_tool ──────────────────────────────────────
    print("\n🔍 STEP 3. rag_tool (컨텍스트 추출)")
    rag_context = await search_rag_context(
        shop_id=SHOP_ID,
        trend_data=real_trend,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=MOCK_RECENT_POSTS
    )

    # ── STEP 4. post_writer ────────────────────────────────────
    print("\n✍️  STEP 4. post_writer (최종 캡션 작성)")
    post_draft = await post_writer_agent(
        shop_id=SHOP_ID,
        trend_data=real_trend,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=MOCK_RECENT_POSTS,
        rag_context=rag_context
    )

    # ── 결과 출력 ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  🚀 최종 통합 테스트 결과")
    print("=" * 60)
    print(f"\n[shop_id]: {SHOP_ID}")
    print(f"\n[오늘의 트렌드]:\n{real_trend.get('trend', '')[:100]}...")
    print(f"\n[생성된 인스타 캡션]:\n{post_draft['caption']}")
    print(f"\n[해시태그]: {' '.join(post_draft['hashtags'])}")
    print(f"\n[CTA]: {post_draft.get('cta', '')}")
    print(f"\n[사용된 사진]: {len(selected_photos)}장")
    for p in selected_photos:
        print(f"  - {p['id']} | {p.get('blob_url', '')}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_test())