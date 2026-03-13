from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from pydantic import BaseModel
import uuid

from services.cosmos_db import get_all_photos_by_shop   # 전체사진 조회
from services.cosmos_db import get_photos_by_album      # 앨범 상세 조회
from services.cosmos_db import get_album_list           # 앨범 목록 조회
from services.cosmos_db import save_album               # 새 앨범 만들기
from services.cosmos_db import delete_album_data        # 앨범 삭제
from services.cosmos_db import delete_photo_data        # 사진 삭제

#router = APIRouter(prefix="/api/photos", tags=["Photos"])
router = APIRouter()
# --- Pydantic 모델 (새 앨범 만들 때 사용) ---
class AlbumCreateRequest(BaseModel):
    shop_id: str
    album_id: str
    album_name: str
    photo_ids: List[str]
    description: str = ""

# 1. 전체 사진 조회 (프론트엔드 Photos 화면용)
@router.get("/all/{shop_id}")
async def read_all_photos(shop_id: str):
    photos = get_all_photos_by_shop(shop_id) # 함수 호출
    return {"photos": photos}

# 2. 앨범 목록 조회 (프론트엔드 Album 화면용)
@router.get("/albums/{shop_id}")
async def read_albums(shop_id: str):
    albums = get_album_list(shop_id) # 함수 호출
    return {"albums": albums}

# 3. 특정 앨범 내 사진 조회
@router.get("/albums/{shop_id}/{album_id}")
async def read_album_photos(shop_id: str, album_id: str):
    photos = get_photos_by_album(shop_id, album_id) # 함수 호출
    return {"album_id": album_id, "photos": photos}

# 4. 새 앨범 생성 (사진들을 묶어서 앨범으로 저장)
@router.post("/albums")
async def create_album(req: AlbumCreateRequest):
    # photo_ids 리스트를 함수 형식에 맞게 변환 (dict 형태의 list)
    photo_list = [{"photo_id": pid} for pid in req.photo_ids]
    
    # 🚨 [수정] album_id가 "new"이거나 없으면 새로 생성
    actual_album_id = req.album_id
    if not actual_album_id or actual_album_id == "new":
        actual_album_id = str(uuid.uuid4()) # 새 UUID 생성

    success = save_album(
        shop_id=req.shop_id, 
        album_id=actual_album_id, # None으로 주면 함수에서 자동 생성함
        photo_list=photo_list, 
        album_name=req.album_name,
        description=req.description
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="앨범 저장에 실패했습니다.")
    
    return {"status": "success", "album_id": actual_album_id}

# 앨범 삭제 API
@router.delete("/albums/{shop_id}/{album_id}")
async def delete_album(shop_id: str, album_id: str):
    success = delete_album_data(shop_id, album_id)
    if not success:
        raise HTTPException(status_code=500, detail="앨범 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "앨범이 삭제되었습니다."}

# 사진 삭제 API
@router.delete("/{shop_id}/{photo_id}")
async def delete_photo(shop_id: str, photo_id: str):
    success = delete_photo_data(shop_id, photo_id)
    if not success:
        raise HTTPException(status_code=500, detail="사진 삭제 중 오류가 발생했습니다.")
    return {"status": "success", "message": "사진이 삭제되었습니다."}