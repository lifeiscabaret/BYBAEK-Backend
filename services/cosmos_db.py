from services.cosmos_client import get_cosmos_container
import logging
import uuid
from datetime import datetime, timedelta
from azure.cosmos.errors import CosmosResourceNotFoundError
from services.blob_storage import delete_blob

def update_shop_instagram_info(shop_id: str, insta_data: dict) -> bool:
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
    container = get_cosmos_container("Shop")
    try:
        query = "SELECT c.location, c.city FROM c WHERE c.shop_id = @shop_id"
        parameters = [{"name": "@shop_id", "value": shop_id}]
        items = list(container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True))
        if items:
            city = items[0].get("city") or "서울"
            locale = items[0].get("locale") or "KR"
            return {"city": city, "locale": locale, "timezone_offset": 9}
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
    except Exception as e:
        logging.error(f"위치 정보 조회 실패: {str(e)}")
        return {"city": "서울", "locale": "KR", "timezone_offset": 9}
    
def get_today_web_search_cache(shop_id: str, date_str: str):
    container = get_cosmos_container("Cache")
    cache_id = f"{shop_id}_{date_str}"
    try:
        cache_item = container.read_item(item=cache_id, partition_key=shop_id)
        return cache_item.get("result")
    except Exception:
        return None

def save_web_search_cache(shop_id: str, date_str: str, result: dict) -> bool:
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
    container = get_cosmos_container("Photo")
    raw_url = photo_data['blob_url']
    clean_url = raw_url.split("?")[0]
    photo_id = photo_data['photo_id']

    try:
        # [FIX] 기존 데이터 확인 - passed/failed면 필터링 결과 보존
        try:
            existing = container.read_item(item=photo_id, partition_key=shop_id)
            if existing.get("filter_status") in ("passed", "failed"):
                existing["blob_url"] = clean_url
                existing["updated_at"] = datetime.utcnow().isoformat()
                container.upsert_item(body=existing)
                return True
        except Exception:
            pass  # 신규 사진이면 아래에서 새로 생성

        # 신규 사진 저장
        item = {
            "id": photo_id,
            "shop_id": shop_id,
            "blob_url": clean_url,
            "onedrive_url": photo_data.get('onedrive_url', ''),
            "original_name": photo_data['name'],
            "used_yn": False,
            "is_usable": None,
            "filter_status": "pending",
            "created_at": photo_data['last_modified']
        }
        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"Photo 저장 실패: {str(e)}")
        return False
    
def get_onboarding(shop_id: str) -> dict:
    shop_container = get_cosmos_container("Shop")
    try:
        shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        
        # [FIX] name, insta_user_id 추가 → 프론트 계정 표시에 필요
        allowed_keys = [
            "id", "shop_id", "system_prompt",
            "name",                             # [FIX] MS 계정 이메일 표시용
            "insta_auto_upload_yn", "insta_upload_notice_yn",
            "insta_upload_time", "insta_upload_time_slot",
            "insta_notice_time", "insta_review_bfr_upload_yn",
            "insta_user_id",                    # [FIX] 인스타 연결 여부 확인용
            "brand_tone", "preferred_styles", "exclude_conditions",
            "hashtag_style", "cta", "shop_intro",
            "forbidden_words", "locale", "city", "language",
            "is_kakao_connected", "is_insta_connected", "is_gmail_connected",
            "rag_reference", "is_ms_connected", "owner_email", "district"
        ]

        filtered_shop_info = {k: shop_item.get(k) for k in allowed_keys if k in shop_item}
        
        return {"shop_info": filtered_shop_info}
    except Exception as e:
        logging.error(f"온보딩 데이터 필터링 조회 실패 (shop_id: {shop_id}): {str(e)}")
        return None

def get_all_photos_by_shop(shop_id: str) -> list:
    container = get_cosmos_container("Photo")
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    try:
        photos = list(container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True))
        return photos
    except Exception as e:
        logging.error(f"Photo 조회 중 오류 발생: {str(e)}")
        return []
    
def get_photos_by_album(shop_id: str, album_id: str) -> list:
    album_container = get_cosmos_container("Album")
    photo_container = get_cosmos_container("Photo")
    try:
        album = album_container.read_item(item=album_id, partition_key=shop_id)
        photo_ids = album.get("photo_ids", [])
        if not photo_ids:
            return []
        photo_details = []
        for pid in photo_ids:
            try:
                actual_id = pid["photo_id"] if isinstance(pid, dict) else pid
                photo_item = photo_container.read_item(item=actual_id, partition_key=shop_id)
                photo_details.append({
                    "id": photo_item.get("id"),
                    "blob_url": photo_item.get("blob_url"),
                    "original_name": photo_item.get("original_name"),
                    "created_at": photo_item.get("created_at")
                })
            except Exception:
                continue
        return photo_details
    except Exception as e:
        logging.error(f"앨범 내 사진 상세 조회 실패 (album_id: {album_id}): {str(e)}")
        return []

def save_album(shop_id: str, album_id: str, photo_list: list, album_name: str = "미분류 앨범", description: str = "") -> bool:
    album_container = get_cosmos_container("Album")
    try:
        current_time_iso = datetime.utcnow().isoformat()
        new_photo_ids = [p.get('photo_id') or p.get('id') for p in photo_list if p.get('photo_id') or p.get('id')]
        try:
            album_item = album_container.read_item(item=album_id, partition_key=shop_id)
            album_item["photo_ids"] = new_photo_ids
            album_item["album_name"] = album_name
            album_item["description"] = description
            album_item["updated_at"] = current_time_iso
        except Exception:
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
    album_container = get_cosmos_container("Album")
    photo_container = get_cosmos_container("Photo")
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id ORDER BY c.created_at DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    try:
        albums = list(album_container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False))
        for album in albums:
            photo_ids = album.get("photo_ids", [])
            album["description"] = album.get("description", "")
            album["photo_count"] = len(photo_ids)
            album["thumbnail_url"] = None
            if photo_ids:
                first_photo_id = photo_ids[0]
                try:
                    photo_item = photo_container.read_item(item=first_photo_id, partition_key=shop_id)
                    album["thumbnail_url"] = photo_item.get("blob_url")
                except Exception:
                    album["thumbnail_url"] = None
        return albums
    except Exception as e:
        logging.error(f"앨범 목록 조회 실패 (shop_id: {shop_id}): {str(e)}")
        return []


def save_onboarding(shop_id: str, data: dict) -> bool:
    shop_container = get_cosmos_container("Shop")
    now_iso = datetime.utcnow().isoformat()

    allowed_shop_keys = [
        "system_prompt", "insta_auto_upload_yn", "insta_upload_notice_yn",
        "insta_upload_time", "insta_upload_time_slot",
        "insta_notice_time", "insta_review_bfr_upload_yn",
        "brand_tone", "preferred_styles", "exclude_conditions",
        "hashtag_style", "cta", "shop_intro",
        "forbidden_words", "locale", "city", "language",
        "is_kakao_connected", "is_insta_connected", "is_gmail_connected",
        "rag_reference", "is_ms_connected", "owner_email", "district"
    ]

    try:
        try:
            shop_item = shop_container.read_item(item=shop_id, partition_key=shop_id)
        except Exception:
            shop_item = {"id": shop_id, "shop_id": shop_id, "created_at": now_iso}
            
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
    post_container = get_cosmos_container("Post")
    photo_container = get_cosmos_container("Photo")
    query = "SELECT * FROM c WHERE c.shop_id = @shop_id AND c.status = 'success' ORDER BY c._ts DESC"
    parameters = [{"name": "@shop_id", "value": shop_id}]
    posts = list(post_container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False))
    for post in posts:
        photo_ids = post.get("photo_ids", [])
        post["thumbnail_url"] = None
        if photo_ids:
            first_photo_id = photo_ids[0]
            try:
                photo_item = photo_container.read_item(item=first_photo_id, partition_key=shop_id)
                post["thumbnail_url"] = photo_item.get("blob_url")
            except Exception:
                post["thumbnail_url"] = "https://via.placeholder.com/150"
    return posts

def get_post_detail_data(post_id: str, shop_id: str) -> dict:
    post_container = get_cosmos_container("Post")
    photo_container = get_cosmos_container("Photo")
    try:
        post = post_container.read_item(item=post_id, partition_key=shop_id)
        photo_ids = post.get("photo_ids", [])
        photo_details = []
        for pid in photo_ids:
            try:
                photo_item = photo_container.read_item(item=pid, partition_key=shop_id)
                photo_details.append({"id": pid, "blob_url": photo_item.get("blob_url")})
            except Exception:
                continue
        post["photo_details"] = photo_details
        post["photo_urls"] = [p["blob_url"] for p in photo_details]
        return post
    except Exception as e:
        logging.error(f"게시물 상세 조회 실패 (post_id: {post_id}): {str(e)}")
        return None

def save_post_data(shop_id: str, post_data: dict) -> bool:
    container = get_cosmos_container("Post")
    try:
        current_time = datetime.utcnow()
        current_time_iso = current_time.isoformat()
        post_id = post_data.get('id')
        if not post_id:
            new_uuid = str(uuid.uuid4())
            post_id = f"post_{new_uuid}"
            post_data['id'] = post_id
            post_data['created_at'] = current_time_iso
        else:
            try:
                existing_item = container.read_item(item=post_id, partition_key=shop_id)
                post_data['created_at'] = existing_item.get('created_at', current_time_iso)
            except Exception:
                post_data['created_at'] = current_time_iso

        post_data['shop_id'] = shop_id
        post_data['status'] = 'success'
        post_data['updated_at'] = current_time_iso
        post_data['trend_score'] = post_data.get('trend_score', 0)
        post_data['caption_score'] = post_data.get('caption_score', 0)
        post_data['model_used'] = post_data.get('model_used', '')
        post_data['elapsed_seconds'] = post_data.get('elapsed_seconds', 0)

        if 'review_deadline' not in post_data:
            deadline = current_time + timedelta(hours=24)
            post_data['review_deadline'] = deadline.isoformat()
            
        post_data['result_notified'] = False
        post_data['review_action'] = 'pending'

        container.upsert_item(body=post_data)
        return True
    except Exception as e:
        logging.error(f"마케팅 데이터 저장 실패 (shop_id: {shop_id}): {str(e)}")
        return False

def get_top_photos(shop_id: str, limit: int = 20) -> list:
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
    container = get_cosmos_container("Post")
    query = """
        SELECT TOP @limit * FROM c 
        WHERE c.shop_id = @shop_id AND c.status = 'success' 
        ORDER BY c._ts DESC
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, {"name": "@limit", "value": limit}]
    try:
        items = container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False)
        return list(items)
    except Exception as e:
        logging.error(f"recent_posts 조회 실패: {str(e)}")
        return []

def save_draft(
    shop_id: str, post_id: str, caption: str, hashtags: list,
    photo_ids: list, cta: str, review_action: str,
    caption_score: float = 0.0, retry_count: int = 0, model_used: str = "mini"
) -> bool:
    container = get_cosmos_container("Post")
    now_iso = datetime.utcnow().isoformat()
    try:
        try:
            existing_item = container.read_item(item=post_id, partition_key=shop_id)
            created_at = existing_item.get('created_at', now_iso)
        except Exception:
            created_at = now_iso

        draft_data = {
            "id": post_id, "shop_id": shop_id,
            "caption": caption, "hashtags": hashtags,
            "photo_ids": photo_ids, "cta": cta,
            "created_at": created_at, "updated_at": now_iso,
            "review_action": review_action, "reviewed_at": now_iso,
            "status": "success" if review_action in ['ok', 'auto_approved'] else "pending",
            "metrics": {"caption_score": caption_score, "retry_count": retry_count, "model_used": model_used}
        }
        container.upsert_item(body=draft_data)
        logging.info(f"초안 저장 완료 → post_id={post_id}, score={caption_score}, retry={retry_count}, model={model_used}")
        return True
    except Exception as e:
        logging.error(f"초안 저장 실패 (post_id: {post_id}): {str(e)}")
        return False

def get_draft(shop_id: str, post_id: str) -> dict:
    container = get_cosmos_container("Post")
    try:
        draft_item = container.read_item(item=post_id, partition_key=shop_id)
        logging.info(f"초안 조회 성공 (post_id: {post_id})")
        return draft_item
    except Exception as e:
        logging.error(f"초안 조회 실패 (post_id: {post_id}): {str(e)}")
        return None

def save_photo_meta(shop_id: str, doc: dict) -> bool:
    container = get_cosmos_container("Photo")
    try:
        photo_id = doc.get('id')
        existing_item = container.read_item(item=photo_id, partition_key=shop_id)
        existing_item.update({
            "fade_cut_score": doc.get("fade_cut_score", 0),
            "detected_angle": doc.get("detected_angle", "unknown"),
            "style_tags": doc.get("stage2_tags", doc.get("style_tags", [])),
            "is_usable": doc.get("is_usable", False),
            "stage1_pass": doc.get("stage1_pass", False),
            "stage2_pass": doc.get("stage2_pass"),
            "fail_reason": doc.get("fail_reason"),
            "filter_status": doc.get("filter_status", "failed"),
            "updated_at": datetime.utcnow().isoformat()
        })
        container.upsert_item(body=existing_item)
        return True
    except Exception as e:
        logging.error(f"사진 메타데이터 업데이트 실패: {str(e)}")
        return False

def delete_album_data(shop_id: str, album_id: str) -> bool:
    container = get_cosmos_container("Album")
    try:
        container.delete_item(item=album_id, partition_key=shop_id)
        return True
    except CosmosResourceNotFoundError:
        return True
    except Exception as e:
        logging.error(f"앨범 삭제 실패 (album_id: {album_id}): {str(e)}")
        return False

def delete_photo_data(shop_id: str, photo_id: str) -> bool:
    photo_container = get_cosmos_container("Photo")
    try:
        photo_item = photo_container.read_item(item=photo_id, partition_key=shop_id)
        blob_url = photo_item.get("blob_url")
        if blob_url:
            from services.blob_storage import CONTAINER_NAME
            prefix = f"https://bybaekstorage.blob.core.windows.net/{CONTAINER_NAME}/"
            clean_url = blob_url.split("?")[0]
            file_name = clean_url[len(prefix):] if clean_url.startswith(prefix) else clean_url.split("/")[-1]
            delete_blob(file_name)
        photo_container.delete_item(item=photo_id, partition_key=shop_id)
        remove_photo_from_all_albums(shop_id, photo_id)
        return True
    except CosmosResourceNotFoundError:
        return True
    except Exception as e:
        logging.error(f"사진 삭제 실패 (photo_id: {photo_id}): {str(e)}")
        return False
    
def remove_photo_from_all_albums(shop_id: str, photo_id: str):
    album_container = get_cosmos_container("Album")
    try:
        query = "SELECT * FROM c WHERE c.shop_id = @shop_id"
        parameters = [{"name": "@shop_id", "value": shop_id}]
        albums = list(album_container.query_items(query=query, parameters=parameters, enable_cross_partition_query=False))
        for album in albums:
            existing_ids = album.get("photo_ids", [])
            if photo_id in existing_ids:
                album["photo_ids"] = [pid for pid in existing_ids if pid != photo_id]
                album["updated_at"] = datetime.utcnow().isoformat()
                album_container.upsert_item(body=album)
    except Exception as e:
        logging.error(f"앨범 내 사진 참조 제거 중 오류 발생: {str(e)}")

def get_album(shop_id: str, album_id: str) -> dict:
    album_container = get_cosmos_container("Album")
    try:
        return album_container.read_item(item=album_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"단일 앨범 조회 실패 (album_id: {album_id}): {str(e)}")
        return None
    
def get_photo_by_id(shop_id: str, photo_id: str) -> dict:
    photo_container = get_cosmos_container("Photo")
    try:
        return photo_container.read_item(item=photo_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"단일 사진 조회 실패 (photo_id: {photo_id}): {str(e)}")
        return None
    
def save_auth(shop_id: str, auth_data: dict):
    container = get_cosmos_container("Shop")
    try:
        try:
            item = container.read_item(item=shop_id, partition_key=shop_id)
        except Exception:
            item = {"id": shop_id, "shop_id": shop_id}
        item.update(auth_data)
        item["updated_at"] = datetime.utcnow().isoformat()
        container.upsert_item(item)
        return True
    except Exception as e:
        logging.error(f"인증 정보 저장 실패 ({shop_id}): {str(e)}")
        return False

def get_auth(shop_id: str):
    container = get_cosmos_container("Shop")
    try:
        return container.read_item(item=shop_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"인증 정보 조회 실패 ({shop_id}): {str(e)}")
        return None
    
def get_shop_info(shop_id: str) -> dict:
    container = get_cosmos_container("Shop")
    try:
        return container.read_item(item=shop_id, partition_key=shop_id)
    except Exception as e:
        logging.error(f"상점 설정 조회 실패 (shop_id: {shop_id}): {str(e)}")
        return None
 
def update_schedule_settings(shop_id: str, upload_time: str, timezone: str = "Asia/Seoul") -> bool:
    container = get_cosmos_container("Shop")
    try:
        shop_item = container.read_item(item=shop_id, partition_key=shop_id)
        shop_item["insta_upload_time"] = upload_time
        shop_item["insta_upload_time_slot"] = timezone
        shop_item["updated_at"] = datetime.utcnow().isoformat()
        container.upsert_item(body=shop_item)
        return True
    except Exception as e:
        logging.error(f"스케줄 설정 저장 실패 (shop_id: {shop_id}): {str(e)}")
        return False

def get_all_shops() -> list:
    container = get_cosmos_container("Shop")
    query = "SELECT c.id, c.shop_id, c.insta_upload_time, c.insta_auto_upload_yn FROM c"
    try:
        return list(container.query_items(query=query, enable_cross_partition_query=True))
    except Exception as e:
        logging.error(f"전체 샵 목록 조회 실패: {str(e)}")
        return []
    
def get_recent_drafts_with_scores(shop_id: str, limit: int = 20) -> list:
    container = get_cosmos_container("Post")
    query = f"""
        SELECT c.id, c.caption, c.hashtags, c.metrics.caption_score AS caption_score, c.model_used, c.created_at
        FROM c
        WHERE c.shop_id = '{shop_id}'
        AND IS_DEFINED(c.metrics.caption_score)
        ORDER BY c._ts DESC
        OFFSET 0 LIMIT {limit}
    """
    return list(container.query_items(query=query, enable_cross_partition_query=True))


def save_rejection_log(shop_id: str, doc: dict) -> None:
    container = get_cosmos_container("RejectionLog")
    container.upsert_item(doc)


def get_rejection_logs(shop_id: str, limit: int = 50) -> list:
    container = get_cosmos_container("RejectionLog")
    query = f"""
        SELECT * FROM c
        WHERE c.shop_id = '{shop_id}'
        ORDER BY c._ts DESC
        OFFSET 0 LIMIT {limit}
    """
    return list(container.query_items(query=query, enable_cross_partition_query=True))