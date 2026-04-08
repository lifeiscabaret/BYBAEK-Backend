"""
파일명: workers/photo_queue_worker.py
역할: Azure Queue에서 사진 배치를 꺼내 업로드 + 필터링 처리하는 워커

[주요 흐름]
30초마다 폴링 → 큐에서 배치 메시지 꺼내기
→ 사진별 DB 중복 체크
→ Blob Storage 업로드
→ Cosmos DB 저장
→ 필터링 트리거

[실행 방법]
main.py startup 이벤트에서 백그라운드 스레드로 실행:
    from workers.photo_queue_worker import start_worker
    start_worker()
"""

import json
import mimetypes
import os
import threading
import time
import traceback
from urllib.parse import quote

import requests
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueClient, BinaryTransferEncoding

from utils.logging import logger
from services.cosmos_db import save_photo, update_shop_onedrive_info


QUEUE_NAME = "bybaek-photo-sync"
INSTAGRAM_SUPPORTED = {".jpg", ".jpeg", ".png"}
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
POLL_INTERVAL_SECONDS = 30      # 폴링 주기
MAX_MESSAGES_PER_POLL = 10      # 한 번에 꺼낼 메시지 수 (Azure 최대 32)
VISIBILITY_TIMEOUT = 300        # 메시지 처리 시간 여유 (초)


# ──────────────────────────────────────────
# Queue Client
# ──────────────────────────────────────────

def get_queue_client() -> QueueClient:
    return QueueClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
        queue_name=QUEUE_NAME,
        message_encode_policy=BinaryTransferEncoding()
    )


# ──────────────────────────────────────────
# 단일 메시지 처리
# ──────────────────────────────────────────

def process_message(message_body: dict) -> dict:
    """
    큐 메시지 1개(10장 배치) 처리.
    Returns: {"uploaded": N, "skipped": N, "failed": N, "filter_list": [...]}
    """
    shop_id = message_body["shop_id"]
    drive_id = message_body["drive_id"]
    token = message_body["token"]
    container_name = message_body["container_name"]
    photos = message_body["photos"]

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
                    "is_usable": False,        # 필터링 전까지 false
                    "filter_status": "pending"
                })
                filter_list.append({"image_id": photo_id, "blob_url": blob_url})

            logger.info(f"[worker] ✅ 업로드 성공: {name}")
            uploaded += 1

        except ResourceExistsError:
            # Blob이 이미 존재하면 스킵 (정상 케이스)
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
    """
    30초마다 큐를 폴링하며 메시지 처리.
    별도 스레드에서 실행.
    """
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
            filter_map: dict[str, list] = {}  # shop_id → filter_list

            for msg in messages:
                try:
                    body = json.loads(msg.content.decode("utf-8"))
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

                    # 처리 완료 후 큐에서 삭제
                    queue_client.delete_message(msg)
                    processed += 1

                except Exception as e:
                    logger.error(f"[worker] 메시지 처리 실패 (재시도 대기): {e}")
                    traceback.print_exc()
                    # 삭제하지 않으면 visibility_timeout 후 자동으로 재처리됨

            # 배치별 필터링 트리거 (shop_id별로 모아서 한 번에)
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
# 워커 시작 (main.py에서 호출)
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