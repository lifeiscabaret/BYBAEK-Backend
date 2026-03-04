from cosmos_client import get_cosmos_container
import logging
from datetime import datetime, timedelta

def update_shop_instagram_info(shop_id, insta_data):
    """
    ShopInfo 컨테이너에서 해당 shop_id를 찾아 인스타그램 인증 정보를 저장/업데이트합니다.
    """
    container = get_cosmos_container("ShopInfo")
    
    try:
        # 1. 기존 상점 데이터 읽기
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        
        # 2. 인스타그램 관련 정보 추가/업데이트
        shop_item['insta_user_id'] = insta_data.get('user_id')
        shop_item['insta_access_token'] = insta_data.get('access_token')
        shop_item['insta_expires_in'] = insta_data.get('expires_in')
        
        # 3. 저장 (Upsert)
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        import logging
        logging.error(f"인스타그램 정보 DB 저장 실패: {str(e)}")
        return False

def get_shop_location(shop_id: str):
    """
    SurveyQna 컨테이너에서 사장님이 입력한 지역 정보를 가져옵니다.
    """
    container = get_cosmos_container("SurveyQna")
    
    try:
        # shopId 필드로 쿼리 (또는 id가 shop_id와 같다면 read_item 사용)
        query = f"SELECT c.location, c.city FROM c WHERE c.shopId = '{shop_id}'"
        items = list(container.query_items(query=query, enable_cross_partition_query=True))
        
        if items:
            # 설문 데이터 구조에 따라 'city' 또는 'location' 필드를 추출
            # 예: {"city": "부산", "locale": "KR", "timezone_offset": 9}
            city = items[0].get("city") or items[0].get("location") or "서울"
            return {
                "city": city,
                "locale": "KR",
                "timezone_offset": 9
            }
        
        # 데이터가 없을 경우 기본값 반환
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
        
    except Exception as e:
        import logging
        logging.error(f"위치 정보 조회 실패: {str(e)}")
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
    
def get_today_web_search_cache(shop_id: str, date_str: str):
    """
    오늘 이미 검색한 결과가 있는지 캐시 컨테이너에서 조회합니다.
    """
    container = get_cosmos_container("WebSearchCache")
    
    # id를 'shopId_날짜' 형식으로 만들면 조회가 매우 빠릅니다.
    cache_id = f"{shop_id}_{date_str}"
    
    try:
        cache_item = container.read_item(item=cache_id, partition_key=shop_id)
        return cache_item.get("result")
    except Exception:
        # 데이터가 없으면 None 반환
        return None

def save_web_search_cache(shop_id: str, date_str: str, result: dict):
    """
    검색된 결과를 캐시 컨테이너에 저장합니다.
    """
    container = get_cosmos_container("WebSearchCache")
    
    cache_id = f"{shop_id}_{date_str}"
    
    cache_data = {
        "id": cache_id,
        "shopId": shop_id,
        "date": date_str,
        "result": result,
        "expire_at": (datetime.now() + timedelta(days=1)).timestamp() # 옵션: TTL 설정용
    }
    
    try:
        container.upsert_item(body=cache_data)
        return True
    except Exception as e:
        import logging
        logging.error(f"캐시 저장 실패: {str(e)}")
        return False
    


def update_shop_onedrive_info(shop_id, token_info):
    """
    ShopInfo 컨테이너에서 해당 shop_id를 찾아 OneDrive 인증 정보를 업데이트합니다.
    """
    container = get_cosmos_container("ShopInfo")
    
    # 1. 기존 데이터 읽기
    try:
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        
        # 2. 정보 업데이트
        shop_item['one_access_token'] = token_info.get('access_token')
        shop_item['one_refresh_token'] = token_info.get('refresh_token')
        shop_item['one_expires_in'] = token_info.get('expires_in')
        shop_item['one_delta_link'] = token_info.get('delta_link')
        
        # 3. 저장 (Upsert)
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        print(f"Cosmos DB 업데이트 실패: {str(e)}")
        return False

def save_photo_to_album(shop_id, photo_data):
    """
    AI가 선별한 사진 정보를 PhotoAlbum 컨테이너에 저장합니다.
    """
    container = get_cosmos_container("PhotoAlbum")
    
    # 설계 이미지에 맞춘 데이터 구조
    item = {
        "id": photo_data['photo_id'],           # Cosmos DB 필수 id (photoId)
        "shopId": shop_id,                      # 파티션 키
        "album_id": photo_data.get('album_id', 'default'), 
        "album_name": photo_data.get('album_name', 'Promotion'),
        "blob_url": photo_data['blob_url'],     # Blob Storage 주소
        "original_name": photo_data['name'],
        "created_at": photo_data['last_modified']
    }
    
    try:
        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"PhotoAlbum 저장 실패: {str(e)}")
        return False
    
def get_onboarding_data(shop_id):
    """
    ShopInfo(기본정보)와 SurveyQna(설문답변) 데이터를 모두 조회하여 합칩니다.
    """
    shop_container = get_cosmos_container("ShopInfo")
    qna_container = get_cosmos_container("SurveyQna")
    
    try:
        # 1. ShopInfo 조회
        shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        
        # 2. SurveyQna 조회 (shop_id와 일치하는 설문 데이터 가져오기)
        # 쿼리를 사용하거나, 만약 id가 shop_id와 같다면 read_item 사용
        query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}'"
        qna_items = list(qna_container.query_items(query=query, enable_cross_partition_query=True))
        
        # 3. 데이터 합치기
        full_data = {
            "basic_info": {
                "shop_name": shop_item.get("shop_name"),
                "category": shop_item.get("category"),
                "brand_color": shop_item.get("brand_color")
            },
            "survey_answers": qna_items[0] if qna_items else {} # 첫 번째 설문 결과
        }
        return full_data
        
    except Exception as e:
        import logging
        logging.error(f"상세 데이터 조회 실패: {str(e)}")
        return None


def get_all_photos_by_shop(shop_id: str):
    """
    Cosmos DB에서 특정 shop_id를 가진 모든 사진 데이터를 가져옵니다.
    """
    container = get_cosmos_container("PhotoAlbum")
    
    # shopId가 파티션 키라면 성능을 위해 쿼리에 포함하는 것이 좋습니다.
    query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}'"
    
    try:
        # 리스트 형태로 변환하여 반환
        photos = list(container.query_items(
            query=query, 
            enable_cross_partition_query=True
        ))
        return photos
    except Exception as e:
        import logging
        logging.error(f"PhotoAlbum 조회 중 오류 발생: {str(e)}")
        return []
    
def get_photos_by_album(shop_id: str, album_id: str):
    """
    특정 앨범(album_id)에 속한 사진들만 필터링하여 가져옵니다.
    """
    container = get_cosmos_container("PhotoAlbum")
    # shopId와 album_id를 모두 조건으로 걸어 정확한 데이터를 조회합니다.
    query = f"SELECT * FROM c WHERE c.shopId = '{shop_id}' AND c.album_id = '{album_id}'"
    
    try:
        photos = list(container.query_items(query=query, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        return []

# 1. 온보딩 데이터 저장 (ShopInfo)
def save_onboarding_data(shop_id, data, status="PENDING"):
    """
    사용자의 온보딩 설정(나중에/저장)을 ShopInfo 컨테이너에 업데이트합니다.
    """
    container = get_cosmos_container("ShopInfo")
    try:
        # 기존 상점 정보 읽기 시도
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        
        # 전달받은 데이터 업데이트 및 상태 설정
        shop_item.update(data)
        shop_item['onboarding_status'] = status
        
        return container.upsert_item(body=shop_item)
    except Exception as e:
        # 항목이 없을 경우(신규 상점) 새로 생성
        logging.info(f"신규 상점 등록: {shop_id}")
        data['id'] = shop_id
        data['onboarding_status'] = status
        return container.create_item(body=data)

# 2. 상점별 게시물 리스트 조회 (MarketingPost)
def get_post_by_shop(shop_id):
    """
    특정 상점이 생성한 모든 마케팅 게시물을 최신순으로 가져옵니다.
    """
    container = get_cosmos_container("MarketingPost")
    # shop_id가 Partition Key인 경우 가장 효율적인 쿼리입니다.
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id ORDER BY c._ts DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    
    items = container.query_items(
        query=query, 
        parameters=parameters, 
        enable_cross_partition_query=False # Partition Key를 지정했으므로 False가 더 빠름
    )
    return list(items)

# 3. 게시물 단건 상세 조회 (MarketingPost)
def get_post_detail_data(post_id, shop_id):
    """
    게시물의 상세 내용(문구, 이미지 경로 등)을 가져옵니다.
    """
    container = get_cosmos_container("MarketingPost")
    try:
        # Cosmos DB는 ID와 Partition Key(shop_id)가 모두 있어야 가장 빠르게 읽습니다.
        return container.read_item(item=post_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"게시물 상세 조회 실패: {str(e)}")
        return None

# 4. 새 게시물 또는 사진 데이터 저장 (MarketingPost)
def save_post_data(post_data):
    """
    AI가 생성한 게시물이나 사진 데이터를 MarketingPost 컨테이너에 저장합니다.
    """
    container = get_cosmos_container("MarketingPost")
    try:
        # post_data에는 반드시 'id'와 Partition Key인 'shop_id'가 포함되어야 합니다.
        return container.upsert_item(body=post_data)
    except Exception as e:
        logging.error(f"마케팅 데이터 저장 실패: {str(e)}")
        return False
    


# 1. AI 선별 사진 중 점수 높은 순으로 가져오기
def get_top_photos(shop_id: str, limit: int = 20):
    """
    PhotoAlbum에서 사용 가능한(is_usable=true) 사진을 
    fade_cut_score가 높은 순으로 limit개 반환합니다.
    """
    container = get_cosmos_container("PhotoAlbum")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shopId = @shop_id AND c.is_usable = true 
        ORDER BY c.fade_cut_score DESC
    """
    parameters = [
        {"name": "@shop_id", "value": shop_id},
        {"name": "@limit", "value": limit}
    ]
    
    try:
        items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)
        return list(items)
    except Exception as e:
        logging.error(f"top_photos 조회 실패: {str(e)}")
        return []

# 2. 최근 성공한 마케팅 게시물 가져오기
def get_recent_posts(shop_id: str, limit: int = 3):
    """
    MarketingPost에서 업로드 성공한 최근 게시물을 limit개 반환합니다.
    """
    container = get_cosmos_container("MarketingPost")
    # _ts는 Cosmos DB 자체 생성 시간(Timestamp)입니다.
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shop_id = @shop_id AND c.upload_status = 'success' 
        ORDER BY c._ts DESC
    """
    parameters = [
        {"name": "@shop_id", "value": shop_id},
        {"name": "@limit", "value": limit}
    ]
    
    try:
        items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True)
        return list(items)
    except Exception as e:
        logging.error(f"recent_posts 조회 실패: {str(e)}")
        return []

# 3. 게시물 초안 저장 (Pending 상태)
def save_draft(shop_id, post_id, caption, hashtags, photo_ids, cta):
    """
    MarketingPost 컨테이너에 status='pending'으로 초안을 저장합니다.
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

# 4. 사진 메타데이터(AI 평가 결과) 업데이트
def save_photo_meta(shop_id, doc):
    """
    PhotoAlbum에 GPT 평가 결과(점수, 태그 등)를 업데이트(Upsert)합니다.
    doc 안에는 photo_id가 포함되어 있어야 합니다.
    """
    container = get_cosmos_container("PhotoAlbum")
    
    try:
        # 기존 사진 데이터 읽기 (상세 정보를 유지하기 위해)
        photo_id = doc.get('id') or doc.get('photo_id')
        existing_item = container.read_item(item=photo_id, partition_key=shop_id)
        
        # AI 평가 결과 덮어쓰기
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