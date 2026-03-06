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

from services.cosmos_client import get_cosmos_container
import logging
from datetime import datetime, timedelta

def update_shop_instagram_info(shop_id: str, insta_data: dict) -> bool:
    """
    Shop 컨테이너에서 해당 shop_id를 찾아 인스타그램 인증 정보를 저장하거나 업데이트합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        insta_data (dict): user_id, access_token, expires_in을 포함한 인증 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("Shop")
    
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
    Shop 컨테이너에서 사장님이 입력한 지역 정보를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 도시명, 지역, 타임존 정보를 포함한 딕셔너리
    """
    container = get_cosmos_container("Shop")
    
    try:
        query = f"SELECT c.location, c.city FROM c WHERE c.shop_id = '{shop_id}'"
        items = list(container.query_items(query=query, enable_cross_partition_query=True))
        
        if items:
            city = items[0].get("city") or "서울"
            locale = items[0].get("locale") or "KR"
            return {
                "city": city,
                "locale": locale,
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
    container = get_cosmos_container("Cache")
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
    container = get_cosmos_container("Cache")
    cache_id = f"{shop_id}_{date_str}"
    ttl_seconds = 86400
    
    cache_data = {
        "id": cache_id,
        "shop_id": shop_id,
        "date": date_str,
        "result": result,
        "ttl": ttl_seconds,
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
    container = get_cosmos_container("Shop")
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

def save_photo(shop_id: str, photo_data: dict) -> bool:
    """
    동기화된 사진의 기본 메타데이터를 Photo 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        photo_data (dict): photo_id, blob_url, 파일명 등을 포함한 사진 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("Photo")
    item = {
        "id": photo_data['photo_id'],
        "shop_id": shop_id,
        "blob_url": photo_data['blob_url'], 
        "original_name": photo_data['name'],
        "used_yn": False,
        "created_at": photo_data['last_modified']
    }
    
    try:
        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"Photo 저장 실패: {str(e)}")
        return False
    
def get_onboarding(shop_id: str) -> dict:
    """
    상점 기본 정보와 설문 답변 데이터를 결합하여 전체 온보딩 데이터를 반환합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 기본 정보와 설문 답변이 결합된 데이터 (실패 시 None)
    """
    shop_container = get_cosmos_container("Shop")
    try:
        # Shop 조회
        shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        
        # 허용된 필드 리스트
        allowed_keys = [
            "id", "shop_id", "system_prompt", 
            "insta_auto_upload_yn", "insta_upload_notice_yn", 
            "insta_upload_time", "insta_upload_time_slot", 
            "insta_notice_time", "insta_review_bfr_upload_yn",
            "brand_tone", "preferred_styles", "exclude_conditions", 
            "hashtag_style", "cta", "shop_intro", 
            "forbidden_words", "locale", "city", "language",
            "is_kakao_connected", "is_insta_connected", "is_gmail_connected",
            "rag_reference"
        ]

        filtered_shop_info = {k: shop_item.get(k) for k in allowed_keys if k in shop_item}
        
        return {
            "shop_info": filtered_shop_info
        }
    except Exception as e:
        logging.error(f"온보딩 데이터 필터링 조회 실패 (shop_id: {shop_id}): {str(e)}")
        return None

def get_all_photos_by_shop(shop_id: str) -> list:
    """
    특정 상점에 등록된 모든 사진 데이터를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        list: 조회된 사진 객체 리스트
    """
    container = get_cosmos_container("Photo")
    query = f"SELECT * FROM c WHERE c.shop_id = '{shop_id}'"
    
    try:
        photos = list(container.query_items(query=query, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        logging.error(f"Photo 조회 중 오류 발생: {str(e)}")
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
    container = get_cosmos_container("Album")
    query = f"SELECT * FROM c WHERE c.shop_id = '{shop_id}' AND c.album_id = '{album_id}'"
    
    try:
        photos = list(container.query_items(query=query, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        return []

def save_album(shop_id: str, album_id: str, photo_list: list, album_name: str = "미분류 앨범") -> bool:
    """
    사진들을 Album 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        album_id (str): 앨범 고유 식별자
        photo_list (list): 저장할 사진 객체 리스트 (각 객체는 photoId, blob_url 등을 포함)
        album_name (str): 앨범 이름 (신규 생성 시 사용)

    Returns:
        bool: 저장 성공 여부
    """
    album_container = get_cosmos_container("Album")
    
    try:
        current_time = datetime.utcnow().isoformat()
        saved_photo_ids = []

        try:
            # 기존 앨범 정보 조회
            album_item = album_container.read_item(item=album_id, partition_key=shop_id)
            # 기존 사진 ID 리스트에 중복 없이 추가
            existing_ids = set(album_item.get("photo_ids", []))
            existing_ids.update(saved_photo_ids)
            album_item["photo_ids"] = list(existing_ids)
            album_item["updated_at"] = current_time
        except Exception:
            # 앨범이 없으면 신규 생성
            album_item = {
                "id": album_id,
                "shop_id": shop_id,
                "album_name": album_name,
                "photo_ids": saved_photo_ids,
                "created_at": current_time,
                "updated_at": current_time
            }

        album_container.upsert_item(body=album_item)
        return True

    except Exception as e:
        logging.error(f"앨범별 사진 저장 실패 (shop_id: {shop_id}, albumId: {album_id}): {str(e)}")
        return False

def get_album_list(shop_id: str) -> list:
    """
    특정 상점(shop_id)의 모든 앨범 목록을 최신순으로 조회합니다.
    
    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        
    Returns:
        list: 앨범 객체 리스트 (각 앨범의 photo_ids 개수 포함)
    """
    container = get_cosmos_container("Album")
    
    # 파티션 키(/shop_id)를 사용하여 해당 상점의 모든 앨범 조회
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id ORDER BY c.created_at DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    
    try:
        albums = list(container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=False  # 파티션 키를 지정하므로 False 권장
        ))
        
        # UI에서 보여주기 편하도록 사진 개수(count) 등 추가 정보 가공
        for album in albums:
            album["photo_count"] = len(album.get("photo_ids", []))
            
        return albums
    except Exception as e:
        logging.error(f"앨범 목록 조회 실패 (shop_id: {shop_id}): {str(e)}")
        return []


def save_onboarding(shop_id: str, data: dict) -> bool:
    """
    사용자의 온보딩 설정 및 진행 상태를 Shop 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        data (dict): 온보딩 설정 데이터

    Returns:
        bool: 저장 성공 여부
    """
    shop_container = get_cosmos_container("Shop")

    allowed_shop_keys = [
        "system_prompt", "insta_auto_upload_yn", "insta_upload_notice_yn", 
        "insta_upload_time", "insta_upload_time_slot", 
        "insta_notice_time", "insta_review_bfr_upload_yn",
        "brand_tone", "preferred_styles", "exclude_conditions", 
        "hashtag_style", "cta", "shop_intro", 
        "forbidden_words", "locale", "city", "language",
        "is_kakao_connected", "is_insta_connected", "is_gmail_connected",
        "rag_reference"
    ]

    try:
        # --- Shop 업데이트 ---
        try:
            # 기존 데이터를 먼저 읽어와서 토큰 등 민감 정보를 유지함
            shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        except Exception:
            # 기존 데이터가 없는 경우 신규 생성
            shop_item = {"id": shop_id, "shop_id": shop_id}
            
        # 허용된 필드만 골라서 업데이트
        for key in allowed_shop_keys:
            if key in data:
                shop_item[key] = data[key]
        
        shop_container.upsert_item(body=shop_item)
        
        return True
        
    except Exception as e:
        logging.error(f"온보딩 데이터 저장 실패 (shop_id: {shop_id}): {str(e)}")
        return False

def get_post_by_shop(shop_id: str) -> list:
    """
    상점별로 생성된 마케팅 게시물 전체 목록을 최신순으로 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자

    Returns:
        list: 마케팅 게시물 리스트
    """
    container = get_cosmos_container("Post")
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id AND c.status = 'success' ORDER BY c._ts DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    
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
    container = get_cosmos_container("Post")
    try:
        return container.read_item(item=post_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"게시물 상세 조회 실패: {str(e)}")
        return None

def save_post_data(post_data: dict) -> bool:
    """
    AI가 생성한 마케팅 게시물 데이터를 Post 컨테이너에 저장합니다.

    Args:
        post_data (dict): id, shop_id, 문구, 이미지 경로 등을 포함한 게시물 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("Post")
    try:
        post_data['status'] = 'success'
        container.upsert_item(body=post_data)
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
    container = get_cosmos_container("Photo")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shop_id = @shop_id AND c.is_usable = true 
        ORDER BY c.fade_cut_score DESC
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, {"name": "@limit", "value": limit}]
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
    container = get_cosmos_container("Post")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shop_id = @shop_id AND c.status = 'success' 
        ORDER BY c._ts DESC
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, 
                  {"name": "@limit", "value": limit}]
    try:
        items = container.query_items(
                    query=query, 
                    parameters=parameters, 
                    enable_cross_partition_query=False
                )
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
    container = get_cosmos_container("Post")
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
    container = get_cosmos_container("Photo")
    
    try:
        photo_id = doc.get('id')
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