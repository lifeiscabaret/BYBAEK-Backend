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
from azure.cosmos.errors import CosmosResourceNotFoundError
from services.blob_storage import delete_blob

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
        shop_item['updated_at'] = datetime.utcnow().isoformat()
        
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
        shop_item['updated_at'] = datetime.utcnow().isoformat()
        
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
            "rag_reference", "is_ms_connected", "gmail_address" ,"district"
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
    album_container = get_cosmos_container("Album")
    photo_container = get_cosmos_container("Photo")
    
    try:
        # 1. 먼저 Album 컨테이너에서 해당 앨범 정보를 가져옵니다.
        # id가 album_id이고 partition_key가 shop_id인 아이템 조회
        album = album_container.read_item(item=album_id, partition_key=shop_id)
        photo_ids = album.get("photo_ids", [])
        
        if not photo_ids:
            return []

        # 2. photo_ids에 담긴 ID들로 Photo 컨테이너에서 상세 정보(blob_url 등) 조회
        # 성능을 위해 쿼리문을 사용하여 한 번에 가져오거나 반복문으로 조회합니다.
        photo_details = []
        for pid in photo_ids:
            try:
                # 앨범에 저장된 pid가 객체 형태일 수도 있으니 처리 (예: {"photo_id": "..."})
                actual_id = pid["photo_id"] if isinstance(pid, dict) else pid
                
                # Photo 컨테이너에서 사진 상세 정보 조회
                photo_item = photo_container.read_item(item=actual_id, partition_key=shop_id)
                photo_details.append({
                    "id": photo_item.get("id"),
                    "blob_url": photo_item.get("blob_url"),
                    "original_name": photo_item.get("original_name"),
                    "created_at": photo_item.get("created_at")
                })
            except Exception:
                # 특정 사진을 찾을 수 없는 경우(삭제 등) 건너뜁니다.
                continue
                
        return photo_details

    except Exception as e:
        logging.error(f"앨범 내 사진 상세 조회 실패 (album_id: {album_id}): {str(e)}")
        return []

def save_album(shop_id: str, album_id: str, photo_list: list, album_name: str = "미분류 앨범", description: str = "") -> bool:
    """
    사진들을 Album 컨테이너에 저장합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        album_id (str): 앨범 고유 식별자
        photo_list (list): 저장할 사진 객체 리스트 (각 객체는 photoId, blob_url 등을 포함)
        album_name (str): 앨범 이름 (신규 생성 시 사용)
        description (str) : 앨범 설명

    Returns:
        bool: 저장 성공 여부
    """
    album_container = get_cosmos_container("Album")
    try:
        current_time_iso = datetime.utcnow().isoformat()
         # 기존 photo_list
        # new_photo_ids = [p.get('photo_id') for p in photo_list if p.get('photo_id')]

        # 변경 예정
        new_photo_ids = [p.get('photo_id') or p.get('id') for p in photo_list if p.get('photo_id') or p.get('id')]

        try:
            album_item = album_container.read_item(item=album_id, partition_key=shop_id)
            # [수정 포인트] set.update() 대신 프론트가 보낸 리스트로 통째로 교체합니다.
            album_item["photo_ids"] = new_photo_ids 
            album_item["album_name"] = album_name
            album_item["description"] = description
            album_item["updated_at"] = current_time_iso
        except Exception:
            # 신규 생성 로직은 동일
            album_item = {
                "id": album_id,
                "shop_id": shop_id,
                "album_name": album_name,
                "description": description,
                "photo_ids": new_photo_ids,
                "created_at": current_time_iso,
                "updated_at": current_time_iso
            }

        album_container.upsert_item(body=album_item)
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False

def get_album_list(shop_id: str) -> list:
    """
    특정 상점(shop_id)의 모든 앨범 목록을 최신순으로 조회합니다.
    
    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        
    Returns:
        list: 앨범 객체 리스트 (각 앨범의 photo_ids 개수 포함)
    """
    album_container = get_cosmos_container("Album")
    photo_container = get_cosmos_container("Photo")
    
    # 파티션 키(/shop_id)를 사용하여 해당 상점의 모든 앨범 조회
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id ORDER BY c.created_at DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    
    try:
        albums = list(album_container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=False  # 파티션 키를 지정하므로 False 권장
        ))
        
        for album in albums:
            photo_ids = album.get("photo_ids", [])
            album["description"] = album.get("description", "")
            album["photo_count"] = len(photo_ids)
            album["thumbnail_url"] = None  # 기본값은 없음
            
            # 1. 앨범에 사진이 최소 한 장이라도 있다면
            if photo_ids:
                first_photo_id = photo_ids[0]
                try:
                    # 2. 첫 번째 사진의 정보를 Photo 컨테이너에서 조회
                    # (id가 item ID이고 shop_id가 파티션 키인 경우)
                    photo_item = photo_container.read_item(item=first_photo_id, partition_key=shop_id)
                    album["thumbnail_url"] = photo_item.get("blob_url")
                except Exception:
                    # 사진을 못 찾으면 그냥 썸네일 없이 진행
                    album["thumbnail_url"] = None
            
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
    now_iso = datetime.utcnow().isoformat() # 현재 시간 (UTC)

    allowed_shop_keys = [
        "system_prompt", "insta_auto_upload_yn", "insta_upload_notice_yn", 
        "insta_upload_time", "insta_upload_time_slot", 
        "insta_notice_time", "insta_review_bfr_upload_yn",
        "brand_tone", "preferred_styles", "exclude_conditions", 
        "hashtag_style", "cta", "shop_intro", 
        "forbidden_words", "locale", "city", "language",
        "is_kakao_connected", "is_insta_connected", "is_gmail_connected",
        "rag_reference", "is_ms_connected", "gmail_address" ,"district"
    ]

    try:
        # --- Shop 업데이트 ---
        try:
            # 기존 데이터를 먼저 읽어와서 토큰 등 민감 정보를 유지함
            shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        except Exception:
            # 기존 데이터가 없는 경우 신규 생성
            shop_item = {"id": shop_id, "shop_id": shop_id, "created_at": now_iso}
            
        # 허용된 필드만 골라서 업데이트
        for key in allowed_shop_keys:
            if key in data:
                shop_item[key] = data[key]
        shop_item["updated_at"] = now_iso
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
    post_container = get_cosmos_container("Post")
    photo_container = get_cosmos_container("Photo")
    # 1. 상점의 성공한 게시물 목록 조회
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id AND c.status = 'success' ORDER BY c._ts DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    posts = list(post_container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False))

    # 2. 각 게시물에 대표 이미지 URL 추가
    for post in posts:
        photo_ids = post.get("photo_ids", [])
        post["thumbnail_url"] = None  # 기본값 설정

        if photo_ids:
            first_photo_id = photo_ids[0]
            try:
                # Photo 컨테이너에서 해당 ID의 문서 조회
                # partition key가 id와 같다면 read_item이 효율적입니다.
                photo_item = photo_container.read_item(item=first_photo_id, partition_key=shop_id)
                post["thumbnail_url"] = photo_item.get("blob_url")
            except Exception:
                # 사진 정보를 찾지 못할 경우 처리 (로그 출력 등)
                post["thumbnail_url"] = "https://via.placeholder.com/150" # 대체 이미지

    return posts

def get_post_detail_data(post_id: str, shop_id: str) -> dict:
    """
    특정 마케팅 게시물의 상세 정보를 조회합니다.

    Args:
        post_id (str): 게시물 고유 식별자
        shop_id (str): 상점 고유 식별자

    Returns:
        dict: 게시물 상세 데이터 (실패 시 None)
    """
    post_container = get_cosmos_container("Post")
    photo_container = get_cosmos_container("Photo")

    try:
        # 1. 기본 게시물 정보 조회
        post = post_container.read_item(item=post_id, partition_key=shop_id)
        photo_ids = post.get("photo_ids", [])
        
        # 2. photo_ids에 해당하는 실제 blob_url 정보들을 수집
        photo_details = []
        for pid in photo_ids:
            try:
                # 각 사진 ID로 Photo 컨테이너 조회
                photo_item = photo_container.read_item(item=pid, partition_key=shop_id)
                photo_details.append({
                    "id": pid,
                    "blob_url": photo_item.get("blob_url")
                })
            except Exception:
                # 사진이 삭제되었거나 없는 경우 건너뜀
                continue
        
        # 3. 게시물 데이터에 상세 사진 정보 포함 (기존 photo_ids는 유지하거나 대체)
        post["photo_details"] = photo_details
        # 프론트 편의를 위해 URL만 모은 리스트도 넣어주면 좋음
        post["photo_urls"] = [p["blob_url"] for p in photo_details]
        
        return post
        
    except Exception as e:
        logging.error(f"게시물 상세 조회 실패 (post_id: {post_id}): {str(e)}")
        return None

def save_post_data(shop_id: str, post_data: dict) -> bool:
    """
    AI가 생성한 마케팅 게시물 데이터를 Post 컨테이너에 저장합니다.

    Args:
        post_data (dict): id, shop_id, 문구, 이미지 경로 등을 포함한 게시물 데이터

    Returns:
        bool: 저장 성공 여부
    """
    container = get_cosmos_container("Post")
    try:
        current_time = datetime.utcnow()
        current_time_iso = current_time.isoformat()
        
        # 1. ID 자동 생성: post_{shop_id}_{날짜_시간}
        # 이미 id가 있으면(수정 건) 그대로 쓰고, 없으면 새로 생성합니다.
        post_id = post_data.get('id')
        if not post_id:
            timestamp = current_time.strftime("%Y%m%d_%H%M%S")
            post_id = f"post_{shop_id}_{timestamp}"
            post_data['id'] = post_id
            post_data['created_at'] = current_time_iso
        else:
            try:
                existing_item = container.read_item(item=post_id, partition_key=shop_id)
                post_data['created_at'] = existing_item.get('created_at', current_time_iso)
            except Exception:
                post_data['created_at'] = current_time_iso

        # 2. 공통 정보 설정
        post_data['shop_id'] = shop_id
        post_data['status'] = 'success'
        post_data['updated_at'] = current_time_iso
        
        # caption 초안에서 수정되는 경우 받아와서 교체 필요
        # hashtags
        # photo_ids
        # cta

        container.upsert_item(body=post_data)
        return True
    except Exception as e:
        logging.error(f"마케팅 데이터 저장 실패 (shop_id: {shop_id}): {str(e)}")
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

    now_iso = datetime.utcnow().isoformat()
    
    # 초안(Draft)은 보통 새로 생성되는 경우가 많지만, 
    # 기존 초안을 덮어쓸 때를 대비해 로직을 구성합니다.
    try:
        try:
            existing_item = container.read_item(item=post_id, partition_key=shop_id)
            created_at = existing_item.get('created_at', now_iso)
        except Exception:
            created_at = now_iso

        draft_data = {
            "id": post_id,
            "shop_id": shop_id,
            "caption": caption,
            "hashtags": hashtags,
            "photo_ids": photo_ids,
            "cta": cta,
            "status": "pending",
            "created_at": created_at,  # 생성 시간 유지
            "updated_at": now_iso       # 수정 시간 갱신
        }
        
        container.upsert_item(body=draft_data)
        return True
    except Exception as e:
        logging.error(f"초안 저장 실패 (post_id: {post_id}): {str(e)}")
        return False

def get_draft(shop_id: str, post_id: str) -> dict:
    """
    저장된 마케팅 게시물 초안 데이터를 조회합니다.

    Args:
        shop_id (str): 상점 고유 식별자 (Partition Key)
        post_id (str): 게시물 고유 식별자 (Item ID)

    Returns:
        dict: 조회된 초안 데이터 객체. 데이터를 찾지 못하거나 에러 발생 시 None 반환.
    """
    container = get_cosmos_container("Post")

    try:
        draft_item = container.read_item(item=post_id, partition_key=shop_id)
        
        logging.info(f"초안 조회 성공 (post_id: {post_id})")
        return draft_item

    except Exception as e:
        logging.error(f"초안 조회 실패 (post_id: {post_id}): {str(e)}")
        return None

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

def delete_album_data(shop_id: str, album_id: str) -> bool:
    """
    Cosmos DB에서 특정 앨범 데이터를 삭제합니다.
    """
    container = get_cosmos_container("Album")
    try:
        # partition_key(shop_id)를 지정하여 삭제
        container.delete_item(item=album_id, partition_key=shop_id)
        return True
    except CosmosResourceNotFoundError:
        logging.warning(f"삭제하려는 앨범이 존재하지 않음 (album_id: {album_id})")
        return True # 이미 없으면 성공으로 간주
    except Exception as e:
        logging.error(f"앨범 삭제 실패 (album_id: {album_id}): {str(e)}")
        return False

def delete_photo_data(shop_id: str, photo_id: str) -> bool:
    """
    Cosmos DB에서 사진 데이터를 삭제하고, 연결된 Blob Storage 파일도 삭제합니다.
    """
    photo_container = get_cosmos_container("Photo")
    
    try:
        # 1. DB에서 사진 정보 먼저 조회 (Blob URL을 알아내기 위해)
        photo_item = photo_container.read_item(item=photo_id, partition_key=shop_id)
        blob_url = photo_item.get("blob_url")

        # 2. Azure Blob Storage에서 실제 파일 삭제 (기존에 만든 서비스 함수 활용)
        if blob_url:
            # URL에서 파일명만 추출하여 삭제 로직 수행
            file_name = blob_url.split("/")[-1]
            delete_blob(file_name) 

        # 3. Cosmos DB에서 사진 데이터 삭제
        photo_container.delete_item(item=photo_id, partition_key=shop_id)
        
        # 4. (추가 로직) 모든 앨범을 돌며 해당 photo_id가 들어있다면 리스트에서 제거
        remove_photo_from_all_albums(shop_id, photo_id)
        
        return True
    except CosmosResourceNotFoundError:
        return True
    except Exception as e:
        logging.error(f"사진 삭제 실패 (photo_id: {photo_id}): {str(e)}")
        return False
    
def remove_photo_from_all_albums(shop_id: str, photo_id: str):
    """
    특정 상점의 모든 앨범을 순회하며 삭제된 사진 ID를 제거합니다.
    """
    album_container = get_cosmos_container("Album")
    
    try:
        # 1. 해당 shop_id의 모든 앨범 가져오기
        query = "SELECT * FROM c WHERE c.shop_id = @shop_id"
        parameters = [{"name": "@shop_id", "value": shop_id}]
        
        albums = list(album_container.query_items(
            query=query, 
            parameters=parameters, 
            enable_cross_partition_query=False
        ))

        for album in albums:
            existing_ids = album.get("photo_ids", [])
            
            # 2. 삭제할 photo_id가 포함되어 있는지 확인
            if photo_id in existing_ids:
                # 리스트에서 해당 ID 제거
                updated_ids = [pid for pid in existing_ids if pid != photo_id]
                album["photo_ids"] = updated_ids
                album["updated_at"] = datetime.utcnow().isoformat()
                
                # 3. 변경된 앨범 정보 업데이트
                album_container.upsert_item(body=album)
                logging.info(f"앨범 '{album.get('id')}'에서 사진 '{photo_id}' 제거 완료")

    except Exception as e:
        logging.error(f"앨범 내 사진 참조 제거 중 오류 발생: {str(e)}")