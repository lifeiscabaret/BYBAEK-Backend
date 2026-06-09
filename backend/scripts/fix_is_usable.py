from services.cosmos_client import get_cosmos_container
from datetime import datetime

def fix_photos():
    container = get_cosmos_container("Photo")
    shop_id = "00000000-0000-0000-0718-3a306722d45c"

    query = "SELECT * FROM c WHERE c.shop_id = @shop_id"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    photos = list(container.query_items(
        query=query,
        parameters=parameters,
        enable_cross_partition_query=True
    ))

    fixed = 0
    for photo in photos:
        if photo.get("filter_status") == "passed" and not photo.get("is_usable"):
            photo["is_usable"] = True
            photo["updated_at"] = datetime.utcnow().isoformat()
            container.upsert_item(body=photo)
            fixed += 1
            print(f"복구: {photo['id']}")

    print(f"총 {fixed}개 복구 완료 / 전체 {len(photos)}장")

fix_photos()
