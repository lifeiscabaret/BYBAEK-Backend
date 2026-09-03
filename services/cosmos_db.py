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
            "rag_reference", "is_ms_connected", "owner_email", "district",
            "insta_style_profile", "photo_range_max",  # ← 추가
            "insta_upload_days",
            # feed_style 원천 필드 — 이 allowlist에 없으면 _get_brand_settings가
            # 항상 기본값(2~4줄 / 10개)만 보게 되어 온보딩 선택이 무시된다.
            "caption_length", "hashtag_count",
            # 타겟 고객 설명. 예전엔 프론트가 shop_intro에 합쳐 보내서
            # "샵 차별점 - 첫 문장에 녹여줘" 슬롯에까지 흘러들어갔다 → 별도 필드로 분리.
            "target_customer_text"
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
        "rag_reference", "is_ms_connected", "owner_email", "district",
        "photo_range_max", "insta_upload_days",
        # feed_style 원천 필드 (get_onboarding의 allowed_keys와 반드시 함께 유지)
        "caption_length", "hashtag_count",
        "target_customer_text"
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
    """발행/취소 결과를 Post 문서에 기록한다.

    [중요] 예전엔 post_data로 문서를 통째로 교체(upsert)했기 때문에,
    save_draft()가 남긴 metrics.caption_score가 발행 시점에 전부 사라졌다.
    get_recent_drafts_with_scores()는 IS_DEFINED(c.metrics.caption_score)로
    거르기 때문에, 결과적으로 성과 피드백이 "발행되지 않은 초안"만 보고 있었다.
    → 기존 문서를 읽어 그 위에 이번 호출분만 병합(merge)한다.

    status/review_action도 무조건 'success'/'pending'으로 덮어쓰던 것을
    호출자가 준 값 우선으로 바꿨다. (취소된 초안이 success로 뒤집혀
    get_post_by_shop / get_recent_posts에 발행 게시물로 섞여 들어가던 문제)
    """
    container = get_cosmos_container("Post")
    try:
        current_time = datetime.utcnow()
        current_time_iso = current_time.isoformat()
        post_id = post_data.get('id')

        existing_item = {}
        if not post_id:
            post_id = f"post_{uuid.uuid4()}"
        else:
            try:
                existing_item = container.read_item(item=post_id, partition_key=shop_id) or {}
            except Exception:
                existing_item = {}

        # 기존 문서 위에 이번 호출분만 덮어쓴다 (metrics 등 미전달 필드 보존)
        item = {**existing_item, **post_data}

        item['id'] = post_id
        item['shop_id'] = shop_id
        item['created_at'] = existing_item.get('created_at', current_time_iso)
        item['updated_at'] = current_time_iso

        # 호출자 지정값 > 기존값 > 기본값
        item['status'] = post_data.get('status') or existing_item.get('status') or 'success'
        item['review_action'] = (
            post_data.get('review_action') or existing_item.get('review_action') or 'pending'
        )

        item.setdefault('trend_score', 0)
        item.setdefault('caption_score', 0)
        item.setdefault('model_used', '')
        item.setdefault('elapsed_seconds', 0)
        item.setdefault('metrics', {})
        item.setdefault('result_notified', False)

        if 'review_deadline' not in item:
            item['review_deadline'] = (current_time + timedelta(hours=24)).isoformat()

        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"마케팅 데이터 저장 실패 (shop_id: {shop_id}): {str(e)}")
        return False

def get_top_photos(shop_id: str, limit: int = 50) -> list:
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
        # [FIX] doc에 없는 키는 기존 값 유지 (is_usable, filter_status 덮어쓰기 방지)
        existing_item.update({
            "fade_cut_score": doc.get("fade_cut_score", existing_item.get("fade_cut_score")),
            "detected_angle": doc.get("detected_angle", existing_item.get("detected_angle")),
            "style_tags": doc.get("stage2_tags", doc.get("style_tags", existing_item.get("style_tags", []))),
            "is_usable": doc.get("is_usable") if doc.get("is_usable") is not None else existing_item.get("is_usable"),
            "stage1_pass": doc.get("stage1_pass") if doc.get("stage1_pass") is not None else existing_item.get("stage1_pass"),
            "stage2_pass": doc.get("stage2_pass") if doc.get("stage2_pass") is not None else existing_item.get("stage2_pass"),
            "fail_reason": doc.get("fail_reason", existing_item.get("fail_reason")),
            "filter_status": doc.get("filter_status", existing_item.get("filter_status")),
            "used_at": doc.get("used_at", existing_item.get("used_at")),
            # [FIX] Stage2 분류/점수 결과를 실제 저장 (다운스트림 photo_select 등에서 참조).
            #       기존엔 화이트리스트에 없어 doc에 담겨도 버려졌음 → photo_category 등이 비어있던 근본 원인.
            "photo_category": doc.get("photo_category", existing_item.get("photo_category")),
            "total_score": doc.get("total_score", existing_item.get("total_score")),
            "scores": doc.get("scores", existing_item.get("scores")),
            "analyzed_at": doc.get("analyzed_at", existing_item.get("analyzed_at")),
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
            prefix = f"https://bybaekstore1.blob.core.windows.net/{CONTAINER_NAME}/"
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
    query = "SELECT c.id, c.shop_id, c.insta_upload_time, c.insta_upload_time_slot, c.insta_upload_days, c.insta_auto_upload_yn FROM c"
    try:
        return list(container.query_items(query=query, enable_cross_partition_query=True))
    except Exception as e:
        logging.error(f"전체 샵 목록 조회 실패: {str(e)}")
        return []
    
def get_recent_drafts_with_scores(shop_id: str, limit: int = 20) -> list:
    container = get_cosmos_container("Post")
    query = """
        SELECT c.id, c.caption, c.hashtags, c.metrics.caption_score AS caption_score, c.model_used, c.created_at
        FROM c
        WHERE c.shop_id = @shop_id
        AND IS_DEFINED(c.metrics.caption_score)
        ORDER BY c._ts DESC
        OFFSET 0 LIMIT @limit
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, {"name": "@limit", "value": limit}]
    return list(container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True))


def get_published_media_ids(shop_id: str) -> set:
    """이 샵에서 BYBAEK이 발행한 Instagram media_id 집합.

    온보딩 백필(_backfill_rag_index)이 인스타에서 최근 게시물을 긁어올 때
    BYBAEK이 방금 올린 글까지 "사장님 과거 캡션"으로 재인덱싱하는 문제가 있었다.
    → 백필 시 이 집합을 제외해서 자기강화 루프를 끊는다.
    """
    container = get_cosmos_container("Post")
    query = """
        SELECT c.instagram_media_id FROM c
        WHERE c.shop_id = @shop_id AND IS_DEFINED(c.instagram_media_id)
    """
    parameters = [{"name": "@shop_id", "value": shop_id}]
    try:
        rows = container.query_items(query=query, parameters=parameters,
                                     enable_cross_partition_query=True)
        return {str(r["instagram_media_id"]) for r in rows if r.get("instagram_media_id")}
    except Exception as e:
        logging.error(f"발행 media_id 조회 실패 ({shop_id}): {str(e)}")
        return set()


def get_posts_for_engagement() -> list:
    """실참여 수집 대상 — 인스타에 실제 발행된 게시물 전체 (워커 전용).

    published_at은 2026-08 이후 발행분에만 있으므로, 없으면 created_at으로 폴백한다.
    """
    container = get_cosmos_container("Post")
    query = """
        SELECT c.id, c.shop_id, c.instagram_media_id, c.published_at, c.created_at, c.engagement
        FROM c
        WHERE IS_DEFINED(c.instagram_media_id) AND c.instagram_media_id != null
    """
    try:
        return list(container.query_items(query=query, enable_cross_partition_query=True))
    except Exception as e:
        logging.error(f"참여 수집 대상 조회 실패: {str(e)}")
        return []


def save_post_engagement(shop_id: str, post_id: str, window: str, snapshot: dict) -> bool:
    """Post 문서의 engagement[window]에 참여 지표 스냅샷을 기록한다.

    window: "24h" | "7d" — 발행 후 경과 시점별로 따로 보관한다
            (초기 확산과 롱테일을 구분해야 캡션 효과를 볼 수 있다).
    """
    container = get_cosmos_container("Post")
    try:
        item = container.read_item(item=post_id, partition_key=shop_id)
        engagement = item.get("engagement") or {}
        engagement[window] = snapshot
        item["engagement"] = engagement
        item["engagement_updated_at"] = datetime.utcnow().isoformat()
        container.upsert_item(body=item)
        return True
    except Exception as e:
        logging.error(f"참여 지표 저장 실패 (post_id: {post_id}, window: {window}): {str(e)}")
        return False


def get_posts_with_engagement(shop_id: str, limit: int = 50) -> list:
    """성과 분석용 — 참여 지표가 수집된 게시물."""
    container = get_cosmos_container("Post")
    query = """
        SELECT TOP @limit c.id, c.caption, c.hashtags, c.engagement, c.published_at, c.created_at
        FROM c
        WHERE c.shop_id = @shop_id AND IS_DEFINED(c.engagement)
        ORDER BY c._ts DESC
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, {"name": "@limit", "value": limit}]
    try:
        return list(container.query_items(query=query, parameters=parameters,
                                          enable_cross_partition_query=True))
    except Exception as e:
        logging.error(f"참여 지표 게시물 조회 실패 ({shop_id}): {str(e)}")
        return []


def get_shops_with_instagram() -> list:
    """인스타 장기 토큰을 보유한 샵 목록 (토큰 자동 갱신 잡 전용).

    토큰 값 자체가 필요하므로 SELECT 대상에 포함한다 — 호출부는 workers/insta_token_refresh.py 뿐.
    """
    container = get_cosmos_container("Shop")
    query = """
        SELECT c.id, c.shop_id, c.insta_user_id, c.insta_access_token,
               c.insta_token_expires_at, c.insta_updated_at
        FROM c
        WHERE IS_DEFINED(c.insta_access_token) AND c.insta_access_token != null
    """
    try:
        return list(container.query_items(query=query, enable_cross_partition_query=True))
    except Exception as e:
        logging.error(f"인스타 연동 샵 목록 조회 실패: {str(e)}")
        return []


def save_rejection_log(shop_id: str, doc: dict) -> None:
    container = get_cosmos_container("RejectionLog")
    container.upsert_item(doc)


def get_rejection_logs(shop_id: str, limit: int = 50) -> list:
    container = get_cosmos_container("RejectionLog")
    query = """
        SELECT * FROM c
        WHERE c.shop_id = @shop_id
        ORDER BY c._ts DESC
        OFFSET 0 LIMIT @limit
    """
    parameters = [{"name": "@shop_id", "value": shop_id}, {"name": "@limit", "value": limit}]
    return list(container.query_items(query=query, parameters=parameters, enable_cross_partition_query=True))