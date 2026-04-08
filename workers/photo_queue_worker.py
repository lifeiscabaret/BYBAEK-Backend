import json
import mimetypes
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueClient

from utils.logging import logger
from services.cosmos_db import get_shop, save_photo


QUEUE_NAME = "bybaek-photo-sync"
INSTAGRAM_SUPPORTED = {".jpg", ".jpeg", ".png"}
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MS_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
POLL_INTERVAL_SECONDS = 30
MAX_MESSAGES_PER_POLL = 10
VISIBILITY_TIMEOUT = 300

# 토큰 캐시: shop_id → {"access_token": ..., "expires_at": datetime}
_token_cache: dict = {}


# ──────────────────────────────────────────
# Queue Client
# ──────────────────────────────────────────

def get_queue_client() -> QueueClient:
    return QueueClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
        queue_name=QUEUE_NAME
    )


# ──────────────────────────────────────────
# 토큰 관리
# ──────────────────────────────────────────

def get_access_token(shop_id: str) -> str:
    """
    shop_id로 유효한 access_token 반환.
    캐시된 토큰이 있고 만료 5분 전이면 재사용,
    없거나 만료되면 refresh_token으로 새로 발급.
    """
    now = datetime.now(timezone.utc)

    # 캐시 확인
    cached = _token_cache.get(shop_id)
    if cached and cached["expires_at"] > now + timedelta(minutes=5):
        return cached["access_token"]

    # DB에서 refresh_token 조회
    shop_info = get_shop(shop_id)
    if not shop_info:
        raise RuntimeError(f"[worker] shop_id {shop_id} 없음")

    refresh_token = shop_info.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"[worker] shop_id {shop_id}의 refresh_token 없음. "
            "재로그인 필요 (offline_access 스코프 확인)"
        )

    # Microsoft Token Endpoint로 새 access_token 발급
    client_id = os.getenv("ONEDRIVE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")

    resp = requests.post(
        MS_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/Files.ReadWrite.All offline_access",
        },
        timeout=15
    )

    if resp.status_code >= 400:
        raise RuntimeError(
            f"[worker] 토큰 갱신 실패 ({shop_id}): {resp.status_code} {resp.text[:200]}"
        )

    token_data = resp.json()
    new_access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)

    # 캐시 저장
    _token_cache[shop_id] = {
        "access_token": new_access_token,
        "expires_at": now + timedelta(seconds=expires_in)
    }

    # 새 refresh_token이 발급됐으면 DB 업데이트 (rolling refresh_token)
    new_refresh_token = token_data.get("refresh_token")
    if new_refresh_token and new_refresh_token != refresh_token:
        try:
            from services.cosmos_db import update_shop_onedrive_info
            update_shop_onedrive_info(shop_id, {"refresh_token": new_refresh_token})
        except Exception as e:
            logger.warning(f"[worker] refresh_token DB 업데이트 실패 (무시): {e}")

    logger.info(f"[worker] 토큰 갱신 완료 → shop_id={shop_id}")
    return new_access_token


# ──────────────────────────────────────────
# 단일 메시지 처리
# ──────────────────────────────────────────

def process_message(message_body: dict) -> dict:
    """
    큐 메시지 1개(10장 배치) 처리.
    Returns: {"uploaded": N, "skipped": N, "failed": N, "filter_list": [...], "shop_id": str}
    """
    shop_id = message_body["shop_id"]
    drive_id = message_body["drive_id"]
    container_name = message_body["container_name"]
    photos = message_body["photos"]

    # DB에서 refresh_token으로 access_token 발급
    token = get_access_token(shop_id)

    # Blob 클라이언트 초기화
    blob_service = BlobServiceClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    )
    container_client = blob_service.get_container_client(container_name)

    uploaded = 0
    skipped = 0
    failed = 0
    filter_list = []

    for photo in photos:
        name = photo["name"]
        item_id = photo["item_id"]
        relative_path = photo["relative_path"]
        ext = os.path.splitext(name)[1].lower()

        photo_id = (
            f"photo_{shop_id}_"
            f"{relative_path.replace('/', '_').replace(' ', '_')}"
        )

        try:
            # ── DB 중복 체크 ──
            # TODO: from services.cosmos_db import get_photo_by_id
            # existing = get_photo_by_id(shop_id, photo_id)
            existing = None  # 목업

            if existing:
                logger.info(f"[worker] ⏭️ 중복 스킵: {name}")
                skipped += 1
                continue

            # ── 토큰 만료 시 자동 재발급 ──
            # 배치 처리 중간에 만료될 수 있으므로 매 파일마다 유효성 확인
            token = get_access_token(shop_id)

            # ── Blob 업로드 ──
            content_type = photo.get("mime_type") or ""
            if not content_type:
                content_type, _ = mimetypes.guess_type(name)

            download_url = f"{GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content"
            download_resp = requests.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
                stream=True,
                timeout=300
            )
            if download_resp.status_code >= 400:
                raise RuntimeError(f"Download failed: {download_resp.status_code}")

            content_settings = ContentSettings(content_type=content_type)
            container_client.upload_blob(
                name=relative_path,
                data=download_resp.raw,
                content_settings=content_settings,
                overwrite=False
            )

            blob_url = (
                f"https://bybaekstorage.blob.core.windows.net"
                f"/{container_name}/{quote(relative_path)}"
            )

            # ── Cosmos DB 저장 (Instagram 지원 포맷만) ──
            if ext in INSTAGRAM_SUPPORTED:
                save_photo(shop_id, {
                    "photo_id": photo_id,
                    "blob_url": blob_url,
                    "name": name,
                    "last_modified": photo.get("last_modified", ""),
                    "is_usable": False,
                    "filter_status": "pending"
                })
                filter_list.append({"image_id": photo_id, "blob_url": blob_url})

            logger.info(f"[worker] ✅ 업로드 성공: {name}")
            uploaded += 1

        except ResourceExistsError:
            logger.info(f"[worker] ⏭️ Blob 이미 존재 (스킵): {name}")
            skipped += 1

        except Exception as e:
            logger.error(f"[worker] ❌ 업로드 실패 ({name}): {e}")
            failed += 1

    return {
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
        "filter_list": filter_list,
        "shop_id": shop_id,
    }


# ──────────────────────────────────────────
# 필터링 트리거
# ──────────────────────────────────────────

async def trigger_filter(shop_id: str, filter_list: list):
    if not filter_list:
        return
    try:
        from agents.photo_filter import run_photo_filter
        result = await run_photo_filter(shop_id, filter_list)
        logger.info(
            f"[worker] 필터링 완료 → "
            f"1차 PASS {result.get('stage1_passed', 0)} / "
            f"2차 PASS {result.get('stage2_passed', 0)} / "
            f"전체 {result.get('total', 0)}"
        )
    except Exception as e:
        logger.error(f"[worker] 필터링 실패: {e}")


# ──────────────────────────────────────────
# 폴링 루프
# ──────────────────────────────────────────

def polling_loop():
    """30초마다 큐를 폴링하며 메시지 처리. 별도 스레드에서 실행."""
    import asyncio

    queue_client = get_queue_client()
    logger.info(f"[worker] 큐 워커 시작 → 폴링 주기: {POLL_INTERVAL_SECONDS}초")

    while True:
        try:
            messages = queue_client.receive_messages(
                max_messages=MAX_MESSAGES_PER_POLL,
                visibility_timeout=VISIBILITY_TIMEOUT
            )

            processed = 0
            filter_map: dict[str, list] = {}

            for msg in messages:
                try:
                    body = json.loads(msg.content)
                    result = process_message(body)

                    shop_id = result["shop_id"]
                    if result["filter_list"]:
                        filter_map.setdefault(shop_id, []).extend(result["filter_list"])

                    logger.info(
                        f"[worker] 배치 처리 완료 → "
                        f"업로드 {result['uploaded']} / "
                        f"스킵 {result['skipped']} / "
                        f"실패 {result['failed']}"
                    )

                    queue_client.delete_message(msg)
                    processed += 1

                except Exception as e:
                    logger.error(f"[worker] 메시지 처리 실패 (재시도 대기): {e}")
                    traceback.print_exc()

            if filter_map:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                for shop_id, filter_list in filter_map.items():
                    loop.run_until_complete(trigger_filter(shop_id, filter_list))
                loop.close()

            if processed > 0:
                logger.info(f"[worker] 이번 폴링 처리 완료 → {processed}개 배치")

        except Exception as e:
            logger.error(f"[worker] 폴링 에러: {e}")
            traceback.print_exc()

        time.sleep(POLL_INTERVAL_SECONDS)


# ──────────────────────────────────────────
# 워커 시작
# ──────────────────────────────────────────

def start_worker():
    """
    main.py startup 이벤트에서 호출:
        from workers.photo_queue_worker import start_worker

        @app.on_event("startup")
        async def startup():
            start_worker()
    """
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()
    logger.info("[worker] 큐 워커 스레드 시작 완료")