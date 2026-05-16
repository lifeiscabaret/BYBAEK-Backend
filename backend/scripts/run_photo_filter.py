import asyncio
import sys
import os
sys.path.insert(0, "/Users/lifeiscabaret/bybaek-backend")

from services.cosmos_db import get_all_photos_by_shop
from agents.photo_filter import run_photo_filter
from collections import Counter

SHOP_ID = os.environ.get("TEST_SHOP_ID", "")
if not SHOP_ID:
    print("Usage: TEST_SHOP_ID=xxx python run_photo_filter.py")
    sys.exit(1)

async def main():
    photos = get_all_photos_by_shop(SHOP_ID)
    print(f"[run] DB에서 {len(photos)}장 조회")

    photo_list = []
    for p in photos:
        if p.get("filter_status") == "passed" and p.get("is_usable"):
            continue
        photo_list.append({
            "image_id": p["id"],
            "blob_url": p.get("blob_url", ""),
            "is_usable": p.get("is_usable"),
            "filter_status": p.get("filter_status"),
        })

    print(f"[run] 필터링 대상: {len(photo_list)}장 (이미 통과 제외)")

    if not photo_list:
        print("[run] 필터링 대상 없음 → 기존 통과 사진 통계 출력")
        passed = [p for p in photos if p.get("filter_status") == "passed"]
        failed = [p for p in photos if p.get("filter_status") == "failed"]
        pending = [p for p in photos if p.get("filter_status") not in ("passed", "failed")]
        print(f"\n=== 현재 상태 ===")
        print(f"통과: {len(passed)}장")
        print(f"탈락: {len(failed)}장")
        print(f"대기: {len(pending)}장")
        if passed:
            angles = Counter(p.get("detected_angle", "unknown") for p in passed)
            print(f"\n=== 통과 사진 detected_angle 분포 ===")
            for angle, count in angles.most_common():
                print(f"  {angle}: {count}장")
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
