from fastapi import APIRouter
router = APIRouter()

@router.get("/login")
async def login():
    return {"url": "https://login.microsoftonline.com/..."}

@router.get("/callback")
async def callback(code: str):
    return {"shop_id": "shop_001", "token": "..."}