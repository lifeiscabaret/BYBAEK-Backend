import asyncio
import sys
import os
sys.path.insert(0, "/Users/lifeiscabaret/bybaek-backend")

from services.cosmos_db import get_all_photos_by_shop
from agents.photo_filter import run_photo_filter
from collections import Counter

SHOP_ID = os.environ.get("TEST_SHOP_ID", "")
if not SHOP_ID:
    print("Usage: TEST_SHOP_ID=xxx python run_photo_filter_pending.py")
    sys.exit(1)

async def main():
    photos = get_all_photos_by_shop(SHOP_ID)
    print(f"[run] DB에서 {len(photos)}장 조회")

    # pending 사진만 필터링 대상
    photo_list = []
    for p in photos:
        if p.get("filter_status") == "pending":
            photo_list.append({
                "image_id": p["id"],
                "blob_url": p.get("blob_url", ""),
                "is_usable": p.get("is_usable"),
                "filter_status": p.get("filter_status"),
            })

    print(f"[run] pending 사진: {len(photo_list)}장")

    if not photo_list:
        print("[run] pending 사진 없음")
        return

    result = await run_photo_filter(SHOP_ID, photo_list)

    print(f"\n=== 필터링 결과 ===")
    print(f"전체: {result['total']}장")
    print(f"1차 통과: {result['stage1_passed']}장")
    print(f"2차 통과: {result['stage2_passed']}장")
    print(f"탈락: {len(result.get('failed', []))}장")

    passed_results = result.get("results", [])
    if passed_results:
        angles = Counter(r.get("detected_angle", "unknown") for r in passed_results)
        print(f"\n=== 통과 사진 detected_angle 분포 ===")
        for angle, count in angles.most_common():
            print(f"  {angle}: {count}장")

asyncio.run(main())
