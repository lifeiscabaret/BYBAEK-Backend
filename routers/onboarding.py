"""
온보딩 라우터
- POST /api/onboarding: 스무고개 설문 저장
- GET /api/onboarding/{shop_id}: 온보딩 데이터 조회
- POST /api/onboarding/reference: 레퍼런스 사진 저장
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from typing import List, Optional
# from datetime import datetime
from services.cosmos_db import save_onboarding as save_onboarding_db
from services.cosmos_db import get_onboarding as get_onboarding_db

router = APIRouter()

# class PhotoRange(BaseModel):
#     min: int = 1
#     max: int = 5

# class Schedule(BaseModel):
#     upload_time: str
#     frequency: str
#     photo_range: PhotoRange
#     timezone: str = "Asia/Seoul"

# class OnboardingRequest(BaseModel):
#     shop_id: str
#     brand_tone: str
#     forbidden_words: List[str]
#     cta: str
#     schedule: Schedule
#     preferred_styles: Optional[List[str]] = []
#     upload_mood: Optional[str] = ""

class ReferencePhotoRequest(BaseModel):
    shop_id: str
    photo_ids: List[str]  # 사장님이 선택한 레퍼런스 사진 ID 리스트 (3장)
    label: str = "good"    # "good" 고정 (나쁜 예시는 없음)

# @router.post("")
# async def save_onboarding(req: OnboardingRequest):
#     return {"shop_id": req.shop_id, "status": "success"}

# @router.get("/{shop_id}")
# async def get_onboarding(shop_id: str):
#     return {"shop_id": shop_id, "status": "mock"}

@router.post("/reference")
async def save_reference_photos(req: ReferencePhotoRequest):
    """
    온보딩 단계에서 사장님이 선택한 레퍼런스 사진 3장을 저장합니다.
    
    이 레퍼런스 사진들은 photo_filter.py의 2차 필터링에서
    "이 샵이 선호하는 스타일"을 GPT Vision이 학습하는 데 사용됩니다.
    
    Args:
        req.shop_id: 샵 ID
        req.photo_ids: 레퍼런스로 지정할 사진 ID 리스트 (3장)
        req.label: "good" 고정
    
    Returns:
        {"shop_id": str, "saved_count": int, "status": "success"}
    """
    try:
        from services.cosmos_db import save_album
        
        # 레퍼런스 앨범 정보
        album_id = f"reference_{req.shop_id}"
        album_name = "Reference Photos"
        description = f"photo_filter 2차 필터링 학습용 레퍼런스 ({req.label})"
        
        # photo_list 구성 (save_album이 기대하는 형식)
        photo_list = [{"photo_id": pid} for pid in req.photo_ids]
        
        # save_album 호출
        success = save_album(
            shop_id=req.shop_id,
            album_id=album_id,
            photo_list=photo_list,
            album_name=album_name,
            description=description
        )
        
        if not success:
            raise HTTPException(
                status_code=500,
                detail="레퍼런스 앨범 저장 실패"
            )
        
        return {
            "shop_id": req.shop_id,
            "saved_count": len(req.photo_ids),
            "album_id": album_id,
            "status": "success"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"[onboarding] 레퍼런스 저장 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"레퍼런스 사진 저장 중 오류 발생: {str(e)}"
        )

class OnboardingRequest(BaseModel):
    """
    온보딩 API Request Schema
    프론트엔드의 OnboardingData.ts와 매핑됨
    """
    
    # === PERSONAL (개인화 설정) ===
    # Q1: 샵 느낌 (다중선택 + 직접입력)
    brand_tone: Optional[List[str]] = None
    
    # Q2: 강조하고 싶은 시술 (다중선택 + 직접입력)
    preferred_styles: Optional[List[str]] = None
    
    # Q3: 올리기 싫은 사진 유형 (다중선택 + 직접입력)
    exclude_conditions: Optional[List[str]] = None
    
    # Q4: 해시태그 방향 (다중선택 + 직접입력)
    hashtag_style: Optional[List[str]] = None
    
    # Q5: CTA 문구
    cta: Optional[str] = None
    
    # Q6: 가게 소개 문구
    shop_intro: Optional[str] = None
    
    # Q7: 금지 단어 (쉼표로 구분된 문자열을 배열로 변환해서 저장)
    forbidden_words: Optional[List[str]] = None
    
    # Q8: 기존 인기 게시물 URL/내용 (RAG 데이터)
    rag_reference: Optional[str] = None
    
    # Q10: 샵 위치 (도시)
    city: Optional[str] = None
    
    # === APP (앱 설정) ===
    # Q11: 자동 업로드 활성화 여부 ("예 (추천)" → "Y", "아니오" → "N")
    insta_auto_upload_yn: Optional[str] = None
    
    # Q12: 알람 받을 이메일 주소
    owner_email: Optional[EmailStr] = None  # EmailStr로 자동 검증
    
    # Q13: 업로드 스케줄
    insta_upload_time_slot: Optional[str] = None  # "매일", "평일", "주말" 등
    insta_upload_time: Optional[str] = None       # "10:30 AM" 형식
    
    # Q14: 언어 설정
    language: Optional[str] = None  # "ko" 또는 "en"
    
    # === 연동 상태 (Boolean) ===
    is_ms_connected: Optional[bool] = None
    is_insta_connected: Optional[bool] = None
    
    class Config:
        # JSON 예시를 Swagger에 표시
        json_schema_extra = {
            "example": {
                "brand_tone": ["남성적/클래식", "트렌디/모던"],
                "preferred_styles": ["페이드컷", "슬릭백"],
                "exclude_conditions": ["얼굴 클로즈업"],
                "hashtag_style": ["남성 헤어 전문", "지역명 포함"],
                "cta": "DM으로 예약해주세요!",
                "shop_intro": "10년 경력 바버샵, 남성 전문",
                "forbidden_words": ["디자이너", "헤어샵"],
                "rag_reference": "https://instagram.com/p/...",
                "city": "서울 강남구",
                "insta_auto_upload_yn": "Y",
                "owner_email": "example@gmail.com",
                "insta_upload_time_slot": "매일",
                "insta_upload_time": "10:30 AM",
                "language": "ko",
                "is_ms_connected": True,
                "is_insta_connected": True
            }
        }

@router.post("/{shop_id}")
async def save_onboarding_api(shop_id: str, data: OnboardingRequest):
    """
    온보딩 데이터 저장
    """
    # Pydantic 모델을 dict로 변환 (None 값 제외)
    data_dict = data.model_dump(exclude_none=True)
    
    success = save_onboarding_db(shop_id, data_dict)

    if not success:
        raise HTTPException(status_code=500, detail="온보딩 데이터 저장 실패")

    return {
        "success": True,
        "message": "온보딩 데이터 저장 완료"
    }


@router.get("/{shop_id}")
async def get_onboarding_api(shop_id: str):
    """
    온보딩 데이터 조회.
    shop_info 래퍼를 제거하고 필드를 최상위 레벨로 반환합니다.
    프론트엔드가 data.is_gmail_connected, data.owner_email 등으로 직접 접근합니다.
    """
    result = get_onboarding_db(shop_id)

    if not result:
        raise HTTPException(status_code=404, detail="온보딩 데이터 없음")

    # get_onboarding_db가 {"shop_info": {...}} 형태로 반환하므로 최상위로 평탄화
    shop_info = result.get("shop_info", result)
    return shop_info