from fastapi import APIRouter
router = APIRouter()

@router.post("/upload")
async def upload(post_id: str):
    return {"post_id": post_id, "status": "uploaded"}