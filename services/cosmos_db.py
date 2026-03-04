"""
기능: Cosmos DB(NoSQL) 데이터 접근 및 비즈니스 로직 처리
작성자: jiyeon back
최초 생성: 2026. 02. 24.
버전: 1.0

[Modification Information]
DATE        AUTHOR          NOTE
-----------------------------------------------------------
2026.02.24  jiyeon back     최초 생성 및 기본 CRUD 구현
"""

from cosmos_client import get_cosmos_container
import logging
from datetime import datetime, timedelta

def update_shop_instagram_info(shop_id: str, insta_data: dict) -> bool:
    """
    ShopInfo 컨테이너에서 해당 shop_id를 찾아 인스타그램 인증 정보를 저장하거나 업데이트합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        insta_data (dict): user_id, access_token, expires_in을 포함한 인증 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("ShopInfo")
    
    try:
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        shop_item['insta_user_id'] = insta_data.get('user_id')
        shop_item['insta_access_token'] = insta_data.get('access_token')
        shop_item['insta_expires_in'] = insta_data.get('expires_in')
        
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        logging.error(f"인스타그램 정보 DB 저장 실패: {str(e)}")
        return False

def get_shop_location(shop_id: str) -> dict:
    """
    SurveyQna 컨테이너에서 사장님이 입력한 지역 정보를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 도시명, 지역, 타임존 정보를 포함한 딕셔너리
    """
    container = get_cosmos_container("SurveyQna")
    
    try:
        query = f"SELECT c.location, c.city FROM c WHERE c.shopId = '{shop_id}'"
        items = list(container.query_items(query=query, enable_cross_partition_query=True))
        
        if items:
            city = items[0].get("city") or "서울"
            return {
                "city": city,
                "locale": "KR",
                "timezone_offset": 9
            }
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
    except Exception as e:
        logging.error(f"위치 정보 조회 실패: {str(e)}")
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
    
def get_today_web_search_cache(shop_id: str, date_str: str):
    """
    특정 날짜에 이미 수행된 웹 검색 결과가 있는지 캐시 컨테이너에서 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        date_str (str): 조회 기준 날짜 (YYYY-MM-DD)

    Returns:
        dict or None: 캐시된 검색 결과 또는 데이터가 없을 경우 None
    """
    container = get_cosmos_container("WebSearchCache")
    cache_id = f"{shop_id}_{date_str}"
    
    try:
        cache_item = container.read_item(item=cache_id, partition_key=shop_id)
        return cache_item.get("result")
    except Exception:
        return None

def save_web_search_cache(shop_id: str, date_str: str, result: dict) -> bool:
    """
    웹 검색 결과를 캐시 컨테이너에 저장하여 API 중복 호출을 방지합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        date_str (str): 검색 수행 날짜
        result (dict): 저장할 검색 결과 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("WebSearchCache")
    cache_id = f"{shop_id}_{date_str}"
    cache_data = {
        "id": cache_id,
        "shopId": shop_id,
        "date": date_str,
        "result": result,
        "expire_at": (datetime.now() + timedelta(days=1)).timestamp()
    }
    
    try:
        container.upsert_item(body=cache_data)
        return True
    except Exception as e:
        logging.error(f"캐시 저장 실패: {str(e)}")
        return False

def update_shop_onedrive_info(shop_id: str, token_info: dict) -> bool:
    """
    OneDrive 연동 시 획득한 토큰 정보 및 델타 링크를 상점 정보에 업데이트합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        token_info (dict): 액세스 토큰, 리프레시 토큰 등을 포함한 데이터

    Returns:
        bool: 업데이트 성공 여부
    """
    container = get_cosmos_container("ShopInfo")
    try:
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        shop_item['one_access_token'] = token_info.get('access_token')
        shop_item['one_refresh_token'] = token_info.get('refresh_token')
        shop_item['one_expires_in'] = token_info.get('expires_in')
        shop_item['one_delta_link'] = token_info.get('delta_link')
        
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        logging.error(f"OneDrive 정보 업데이트 실패: {str(e)}")
        return False

def save_photo_to_album(shop_id: str, photo_data: dict) -> bool:
    """
    동기화된 사진의 기본 메타데이터를 PhotoAlbum 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        photo_data (dict): photo_id, blob_url, 파일명 등을 포함한 사진 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("PhotoAlbum")
    item = {
        "id": photo_data['photo_id'],
        "shopId": shop_id,
        "album_id": photo_data.get('album_id', 'default'), 
        "album_name": photo_data.get('album_name', 'Promotion'),
        "blob_url": photo_data['blob_url'],
        "original_name": photo_data['name'],
        "created_at": photo_data['last_modified']
    }
    
    try:
        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"PhotoAlbum 저장 실패: {str(e)}")
        return False
    
def get_onboarding_data(shop_id: str) -> dict:
    """
    상점 기본 정보와 설문 답변 데이터를 결합하여 전체 온보딩 데이터를 반환합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 기본 정보와 설문 답변이 결합된 데이터 (실패 시 None)
    """
    shop_container = get_cosmos_container("ShopInfo")
    qna_container = get_cosmos_container("SurveyQna")
    
    try:
        shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}'"
        qna_items = list(qna_container.query_items(query=query, enable_cross_partition_query=True))
        
        full_data = {
            "basic_info": {
                "shop_name": shop_item.get("shop_name"),
                "category": shop_item.get("category"),
                "brand_color": shop_item.get("brand_color")
            },
            "survey_answers": qna_items[0] if qna_items else {}
        }
        return full_data
    except Exception as e:
        logging.error(f"상세 데이터 조회 실패: {str(e)}")
        return None

def get_all_photos_by_shop(shop_id: str) -> list:
    """
    특정 상점에 등록된 모든 사진 데이터를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        list: 조회된 사진 객체 리스트
    """
    container = get_cosmos_container("PhotoAlbum")
    query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}'"
    
    try:
        photos = list(container.query_items(query=query, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        logging.error(f"PhotoAlbum 조회 중 오류 발생: {str(e)}")
        return []
    
def get_photos_by_album(shop_id: str, album_id: str) -> list:
    """
    특정 앨범 식별자에 속한 사진 데이터만 필터링하여 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        album_id (str): 앨범 고유 식별자

    Returns:
        list: 앨범에 속한 사진 객체 리스트
    """
    container = get_cosmos_container("PhotoAlbum")
    query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}' AND c.album_id = '{album_id}'"
    
    try:
        photos = list(container.query_items(query=query, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        return []

def save_onboarding_data(shop_id: str, data: dict, status: str = "PENDING") -> bool:
    """
    사용자의 온보딩 설정 및 진행 상태를 ShopInfo 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        data (dict): 온보딩 설정 데이터
        status (str): 진행 상태 (PENDING, COMPLETED 등)

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("ShopInfo")
    try:
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        shop_item.update(data)
        shop_item['onboarding_status'] = status
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        logging.info(f"신규 상점 등록: {shop_id}")
        data['id'] = shop_id
        data['onboarding_status'] = status
        container.create_item(body=data)
        return True

def get_post_by_shop(shop_id: str) -> list:
    """
    상점별로 생성된 마케팅 게시물 전체 목록을 최신순으로 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        list: 마케팅 게시물 리스트
    """
    container = get_cosmos_container("MarketingPost")
    query = "SELECT * FROM c WHERE c.shopId = @shopId ORDER BY c._ts DESC"
    parameters = [{"name": "@shopId", "value": shop_id}]
    
    items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False)
    return list(items)

def get_post_detail_data(post_id: str, shop_id: str) -> dict:
    """
    특정 마케팅 게시물의 상세 정보를 조회합니다.

    Args:
        post_id (str): 게시물 고유 식별자
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 게시물 상세 데이터 (실패 시 None)
    """
    container = get_cosmos_container("MarketingPost")
    try:
        return container.read_item(item=post_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"게시물 상세 조회 실패: {str(e)}")
        return None

def save_post_data(post_data: dict) -> bool:
    """
    AI가 생성한 마케팅 게시물 데이터를 MarketingPost 컨테이너에 저장합니다.

    Args:
        post_data (dict): id, shop_id, 문구, 이미지 경로 등을 포함한 게시물 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("MarketingPost")
    try:
        container.upsert_item(body=post_data)
        return True
    except Exception as e:
        logging.error(f"마케팅 데이터 저장 실패: {str(e)}")
        return False

def get_top_photos(shop_id: str, limit: int = 20) -> list:
    """
    사용 가능한 사진 중 AI 평가 점수가 높은 순으로 데이터를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        limit (int): 반환할 최대 사진 수

    Returns:
        list: 고득점 사진 데이터 리스트
    """
    container = get_cosmos_container("PhotoAlbum")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shopId = @shopId AND c.is_usable = true 
        ORDER BY c.fade_cut_score DESC
    """
    parameters = [{"name": "@shopId", "value": shop_id}, {"name": "@limit", "value": limit}]
    try:
        items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)
        return list(items)
    except Exception as e:
        logging.error(f"top_photos 조회 실패: {str(e)}")
        return []

def get_recent_posts(shop_id: str, limit: int = 3) -> list:
    """
    최근 성공적으로 업로드된 마케팅 게시물을 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        limit (int): 조회할 게시물 수

    Returns:
        list: 최근 업로드 성공 게시물 리스트
    """
    container = get_cosmos_container("MarketingPost")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shopId = @shopId AND c.upload_status = 'success' 
        ORDER BY c._ts DESC
    """
    parameters = [{"name": "@shopId", "value": shop_id}, {"name": "@limit", "value": limit}]
    try:
        items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)
        return list(items)
    except Exception as e:
        logging.error(f"recent_posts 조회 실패: {str(e)}")
        return []

def save_draft(shop_id: str, post_id: str, caption: str, hashtags: list, photo_ids: list, cta: str) -> bool:
    """
    마케팅 게시물 확정 전 초안 상태로 데이터를 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        post_id (str): 게시물 고유 식별자
        caption (str): 생성된 캡션 문구
        hashtags (list): 추천 해시태그 리스트
        photo_ids (list): 선택된 사진 ID 리스트
        cta (str): 클릭 유도 문구

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("MarketingPost")
    draft_data = {
        "id": post_id,
        "shop_id": shop_id,
        "caption": caption,
        "hashtags": hashtags,
        "photo_ids": photo_ids,
        "cta": cta,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }
    
    try:
        container.upsert_item(body=draft_data)
        return True
    except Exception as e:
        logging.error(f"초안 저장 실패: {str(e)}")
        return False

def save_photo_meta(shop_id: str, doc: dict) -> bool:
    """
    AI 분석이 완료된 사진의 평가 점수와 태그 정보를 기존 사진 데이터에 업데이트합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        doc (dict): photo_id 및 AI 분석 결과(점수, 태그, 가용성)를 포함한 데이터

    Returns:
        bool: 업데이트 성공 여부
    """
    container = get_cosmos_container("PhotoAlbum")
    
    try:
        photo_id = doc.get('id') or doc.get('photo_id')
        existing_item = container.read_item(item=photo_id, partition_key=shop_id)
        existing_item.update({
            "fade_cut_score": doc.get("fade_cut_score", 0),
            "style_tags": doc.get("style_tags", []),
            "is_usable": doc.get("is_usable", False),
            "updated_at": datetime.utcnow().isoformat()
        })
        container.upsert_item(body=existing_item)
        return True
    except Exception as e:
        logging.error(f"사진 메타데이터 업데이트 실패: {str(e)}")
        return False