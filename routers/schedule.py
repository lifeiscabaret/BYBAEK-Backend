from fastapi import APIRouter
router = APIRouter()

@router.get("/{shop_id}")
async def get_schedule(shop_id: str):
    return {
        "upload_time": "19:00",
        "frequency": "daily",
        "photo_range": {"min": 1, "max": 5},
        "next_upload": "2026-03-04T10:00:00",
        "timezone": "Asia/Seoul"
    }