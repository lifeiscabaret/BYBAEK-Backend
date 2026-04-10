"""
역할: Azure Queue에서 사진 배치를 꺼내 업로드 + 필터링 처리하는 워커

[주요 흐름]
30초마다 폴링 → 큐에서 배치 메시지 꺼내기
→ 사진별 DB 중복 체크
→ HEIC → JPG 자동 변환
→ shop_id 기준 경로 격리 후 Blob Storage 업로드
→ Cosmos DB 저장
→ 필터링 트리거

[변경 이력]
- refresh_token 방식으로 위임 토큰 갱신 (개인 MS 계정 지원, 만료 문제 해결)
- photo_id에 hashlib.md5 해시 적용 (Cosmos DB illegal chars 해결)
- shop_id 기준 경로 격리 (photos/{shop_id}/{hash}.jpg)
- HEIC/HEIF → JPG 자동 변환 (pillow-heif)
- SAS URL 기반 프라이빗 Blob 접근

[실행 방법]
main.py lifespan에서 백그라운드 스레드로 실행:
    from workers.photo_queue_worker import start_worker
    start_worker()
"""

import hashlib
import io
import json
import mimetypes
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import msal
import requests
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)
from azure.storage.queue import QueueClient

from utils.logging import logger
from services.cosmos_db import save_photo, get_photo_by_id


# 상수
QUEUE_NAME = "bybaek-photo-sync"
INSTAGRAM_SUPPORTED = {".jpg", ".jpeg", ".png"}
HEIC_FORMATS = {".heic", ".heif"}
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
POLL_INTERVAL_SECONDS = 30
MAX_MESSAGES_PER_POLL = 10
VISIBILITY_TIMEOUT = 300

# 토큰 발급 (앱 수준 - 만료 없음)
def _get_delegated_token(refresh_token: str) -> str:
    """
    refresh_token으로 위임 토큰 갱신.
    개인 MS 계정 + 회사 계정 모두 지원.
    Files.Read.All Delegated 권한 필요.
    """
    app = msal.ConfidentialClientApplication(
        client_id=os.getenv("AZURE_CLIENT_ID"),
        authority="https://login.microsoftonline.com/common",
        client_credential=os.getenv("AZURE_CLIENT_SECRET"),
    )
    result = app.acquire_token_by_refresh_token(
        refresh_token=refresh_token,
        scopes=["https://graph.microsoft.com/Files.Read.All"]
    )
    if not result.get("access_token"):
        raise RuntimeError(f"[worker] 토큰 갱신 실패: {result.get('error_description', result)}")
    return result["access_token"]



# SAS URL 생성 (프라이빗 Blob 접근용)
def _generate_sas_url(blob_url: str, hours: int = 1) -> str:
    """
    blob_url → 임시 SAS URL 변환.
    photo_filter.py에서 GPT Vision 호출 시 사용.
    """
    # https://bybaekstorage.blob.core.windows.net/photos/shop_id/hash.jpg
    path = blob_url.replace("https://bybaekstorage.blob.core.windows.net/", "")
    parts = path.split("/", 1)
    container = parts[0]
    blob_name = parts[1]

    sas_token = generate_blob_sas(
        account_name="bybaekstorage",
        container_name=container,
        blob_name=blob_name,
        account_key=os.getenv("AZURE_STORAGE_KEY"),
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    return f"{blob_url}?{sas_token}"

# HEIC → JPG 변환
def _convert_heic_to_jpg(raw_bytes: bytes) -> bytes:
    """HEIC/HEIF 바이트 → JPEG 바이트 변환. pillow-heif 필요."""
    try:
        import pillow_heif
        from PIL import Image

        pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(raw_bytes))
        output = io.BytesIO()
        img.convert("RGB").save(output, format="JPEG", quality=95)
        return output.getvalue()
    except Exception as e:
        raise RuntimeError(f"HEIC 변환 실패: {e}")


# Queue Client
def get_queue_client() -> QueueClient:
    return QueueClient.from_connection_string(
        os.getenv("AZURE_STORAGE_CONNECTION_STRING"),
        queue_name=QUEUE_NAME,
    )


# 단일 메시지 처리
def process_message(message_body: dict) -> dict:
    """
    큐 메시지 1개(10장 배치) 처리.

    입력: {shop_id, drive_id, refresh_token, container_name, photos}
    출력: {uploaded, skipped, failed, filter_list, shop_id}
    """
    shop_id = message_body["shop_id"]
    drive_id = message_body["drive_id"]
    refresh_token = message_body.get("refresh_token", "")
    container_name = message_body["container_name"]
    photos = message_body["photos"]

    # refresh_token으로 위임 토큰 갱신 (개인/회사 계정 모두 지원)
    token = _get_delegated_token(refresh_token)

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

        # ✅ photo_id: md5 해시로 Cosmos DB illegal chars 방지
        photo_id = f"photo_{shop_id}_{hashlib.md5(relative_path.encode()).hexdigest()}"

        # ✅ Blob 경로: shop_id 기준 격리
        path_hash = hashlib.md5(relative_path.encode()).hexdigest()
        # HEIC는 변환 후 .jpg로 저장
        target_ext = ".jpg" if ext in HEIC_FORMATS else ext
        isolated_path = f"{shop_id}/{path_hash}{target_ext}"

        try:
            # ── DB 중복 체크 ──
            try:
                existing = get_photo_by_id(shop_id, photo_id)
            except Exception:
                existing = None  # DB 조회 실패 시 중복 아닌 것으로 처리

            if existing:
                logger.info(f"[worker] ⏭️ 중복 스킵: {name}")
                skipped += 1
                continue

            # ── Graph API 다운로드 (위임 토큰 → /me 경로 사용) ──
            download_url = (
                f"{GRAPH_BASE}/drives/{drive_id}"
                f"/items/{item_id}/content"
            )

            download_resp = requests.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=300,
                # ✅ stream=True 제거 → content로 받아야 HEIC 변환 가능
            )
            if download_resp.status_code >= 400:
                raise RuntimeError(f"Download failed: {download_resp.status_code}")

            raw = download_resp.content
            content_type = photo.get("mime_type") or ""

            # ✅ HEIC → JPG 자동 변환
            if ext in HEIC_FORMATS:
                logger.info(f"[worker] 🔄 HEIC 변환 중: {name}")
                raw = _convert_heic_to_jpg(raw)
                name = os.path.splitext(name)[0] + ".jpg"
                content_type = "image/jpeg"
                ext = ".jpg"

            if not content_type:
                content_type, _ = mimetypes.guess_type(name)

            # ── Blob 업로드 (격리 경로) ──
            content_settings = ContentSettings(content_type=content_type)
            container_client.upload_blob(
                name=isolated_path,   # ✅ 격리된 경로 사용
                data=raw,
                content_settings=content_settings,
                overwrite=False,
            )

            blob_url = (
                f"https://bybaekstorage.blob.core.windows.net"
                f"/{container_name}/{quote(isolated_path)}"
            )

            # ── Cosmos DB 저장 (Instagram 지원 포맷만) ──
            if ext in INSTAGRAM_SUPPORTED:
                save_photo(shop_id, {
                    "photo_id": photo_id,
                    "blob_url": blob_url,
                    "name": name,
                    "last_modified": photo.get("last_modified", ""),
                    "is_usable": False,
                    "filter_status": "pending",
                })
                # ✅ SAS URL로 필터링 목록 등록 (프라이빗 Blob 접근)
                sas_url = _generate_sas_url(blob_url, hours=2)
                filter_list.append({"image_id": photo_id, "blob_url": sas_url})

            logger.info(f"[worker] ✅ 업로드 성공: {name} → {isolated_path}")
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

# 필터링 트리거
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
                visibility_timeout=VISIBILITY_TIMEOUT,
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


# 워커 시작
def start_worker():
    """main.py lifespan에서 호출."""
    thread = threading.Thread(target=polling_loop, daemon=True)
    thread.start()
    logger.info("[worker] 큐 워커 스레드 시작 완료")