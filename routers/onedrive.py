"""
OneDrive 사진 동기화 라우터

흐름:
  1. OneDrive 변경분 조회 (Delta API → 신규/변경 사진만)
  2. 1차 필터링 (OpenCV 룰 기반, Blob 저장 전)
  3. 통과한 사진만 Blob Storage 저장
  4. DB(Photo 컨테이너)에 메타 저장
  5. 백그라운드에서 2차 필터링 (GPT Vision)
  6. Delta Link 저장 → 다음 sync는 변경분만 처리

사용자 UX:
  - POST /onedrive/sync-photos → 즉시 응답, 백그라운드 처리
  - GET  /onedrive/sync-status/{shop_id} → 진행상황 폴링
  - 완료 시 이메일 알림
"""

import mimetypes
import os
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

import cv2
import numpy as np
import requests
from urllib.parse import quote
from fastapi import APIRouter, BackgroundTasks, HTTPException, status, Request
from pydantic import BaseModel
from azure.storage.blob import BlobServiceClient, ContentSettings
from utils.logging import logger
from services.cosmos_db import save_photo

router = APIRouter()

GRAPH_BASE          = "https://graph.microsoft.com/v1.0"
PHOTO_EXTENSIONS    = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif", ".tiff"}
INSTAGRAM_SUPPORTED = {".jpg", ".jpeg", ".png"}
KST = timezone(timedelta(hours=9))

# 1차 필터 기준값
STAGE1_LAPLACIAN_MIN  = 40    # 흔들림 (낮을수록 관대)
STAGE1_BRIGHTNESS_MIN = 30    # 최소 밝기
STAGE1_BRIGHTNESS_MAX = 240   # 최대 밝기
STAGE1_SKIN_RATIO_MIN = 2.0   # 피부 비중 최소% (뒷머리 사진도 통과)

# 동기화 진행상황 (서버 메모리, 재시작 시 초기화됨)
_sync_progress: Dict[str, dict] = {}


# ── 요청/응답 모델

class SyncPhotosRequest(BaseModel):
    root_folder_item_id: str = "root"
    force_full_sync: bool = False  # True면 Delta 무시하고 전체 재스캔

class SyncPhotosResponse(BaseModel):
    success: bool
    message: str
    shop_id: str

class SyncStatusResponse(BaseModel):
    shop_id: str
    status: str           # "idle" | "running" | "done" | "error"
    total_scanned: int
    stage1_passed: int
    stage2_passed: int
    message: str


# ── Graph API 헬퍼

def graph_get(url: str, token: str, params: Optional[Dict] = None) -> Dict:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, params=params, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph GET {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def get_user_drive_id(token: str) -> str:
    data = graph_get(f"{GRAPH_BASE}/me/drive", token, params={"$select": "id"})
    return data["id"]


def iter_delta(token: str, drive_id: str, delta_link: Optional[str] = None) -> tuple:
    """
    Delta API로 변경된 파일만 조회.
    delta_link 없으면 전체 조회 (최초 sync).

    Returns: (photo_items: list, next_delta_link: str)
    """
    if delta_link:
        url = delta_link
        logger.info("[onedrive] Delta sync → 변경분만 조회")
    else:
        url = f"{GRAPH_BASE}/drives/{drive_id}/root/delta"
        logger.info("[onedrive] Full sync → 전체 조회")

    params = {"$select": "id,name,folder,file,parentReference,lastModifiedDateTime,deleted"}
    all_items = []
    next_delta_link = None

    while url:
        data = graph_get(url, token, params=params)
        all_items.extend(data.get("value", []))
        next_delta_link = data.get("@odata.deltaLink")
        url = data.get("@odata.nextLink")
        params = None

    # 삭제되지 않은 사진만
    photo_items = [
        item for item in all_items
        if "deleted" not in item and _is_photo(item)
    ]
    return photo_items, next_delta_link


def _is_photo(item: Dict) -> bool:
    if "file" not in item:
        return False
    name = item.get("name", "")
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXTENSIONS:
        return True
    return item.get("file", {}).get("mimeType", "").startswith("image/")


def _get_relative_path(photo: Dict) -> str:
    """OneDrive 아이템의 상대 경로 추출"""
    name = photo["name"]
    parent_ref = photo.get("parentReference", {})
    parent_path = parent_ref.get("path", "")
    if "/root:" in parent_path:
        folder = parent_path.split("/root:")[-1].strip("/")
        if folder:
            return f"{folder}/{name}"
    return name


# ── 1차 필터링 (OpenCV 룰 기반, 비용 없음)

def _stage1_filter(image_bytes: bytes) -> tuple:
    """
    이미지 바이트 → OpenCV 룰 기반 1차 필터링.
    Blob 저장 전에 실행 → 탈락 사진은 Blob에 저장 안 됨.

    Returns: (pass: bool, reason: str)
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if image is None:
            return False, "이미지 디코딩 실패"

        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # 1) 흔들림 체크 (Laplacian variance)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        if laplacian_var < STAGE1_LAPLACIAN_MIN:
            return False, f"초점 흐림 ({laplacian_var:.1f})"

        # 2) 밝기 체크
        avg_brightness = np.mean(gray)
        if avg_brightness < STAGE1_BRIGHTNESS_MIN or avg_brightness > STAGE1_BRIGHTNESS_MAX:
            return False, f"밝기 부적절 ({avg_brightness:.1f})"

        # 3) 바버샵 관련성 (얼굴 감지 + 피부색 비중)
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = face_cascade.detectMultiScale(gray, 1.3, 5)

        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lower_skin = np.array([0, 20, 70], dtype=np.uint8)
        upper_skin = np.array([25, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_skin, upper_skin)
        skin_ratio = (cv2.countNonZero(mask) / (height * width)) * 100

        if len(faces) == 0 and skin_ratio < STAGE1_SKIN_RATIO_MIN:
            return False, f"관련성 낮음 (얼굴 미검출, 피부 {skin_ratio:.1f}%)"

        return True, f"1차 통과 (선명도:{laplacian_var:.0f}, 밝기:{avg_brightness:.0f}, 피부:{skin_ratio:.1f}%)"

    except Exception as e:
        return False, f"분석 오류: {e}"


# ── 백그라운드 동기화 메인

async def _run_sync(
    shop_id: str,
    token: str,
    drive_id: str,
    force_full_sync: bool
):
    """
    백그라운드 동기화:
    1. Delta API로 변경분 조회
    2. 1차 필터 통과한 것만 Blob 저장 + DB 저장
    3. 2차 GPT Vision 필터링
    4. Delta Link 저장
    """
    _sync_progress[shop_id] = {
        "status": "running",
        "total_scanned": 0,
        "stage1_passed": 0,
        "stage2_passed": 0,
        "message": "OneDrive 사진 목록 조회 중..."
    }

    try:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        container_name = os.getenv("AZURE_BLOB_CONTAINER_NAME", "photos")
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = blob_service_client.get_container_client(container_name)

        # Delta Link 조회
        delta_link = None
        if not force_full_sync:
            try:
                from services.cosmos_db import get_shop_info
                shop_info = get_shop_info(shop_id) or {}
                delta_link = shop_info.get("one_delta_link")
            except Exception:
                pass

        # OneDrive 변경분 조회
        photo_items, next_delta_link = iter_delta(token, drive_id, delta_link)
        logger.info(f"[onedrive] 사진 {len(photo_items)}장 조회 완료")
        _sync_progress[shop_id]["message"] = f"사진 {len(photo_items)}장 분석 시작..."

        total_scanned = 0
        stage1_passed = 0
        photo_list_for_stage2 = []

        for photo in photo_items:
            name = photo["name"]
            item_id = photo["id"]
            item_drive_id = photo.get("parentReference", {}).get("driveId", drive_id)

            # Instagram 미지원 포맷 스킵
            ext = os.path.splitext(name)[1].lower()
            if ext not in INSTAGRAM_SUPPORTED:
                total_scanned += 1
                continue

            try:
                # 다운로드 (메모리로)
                download_url = f"{GRAPH_BASE}/drives/{item_drive_id}/items/{item_id}/content"
                resp = requests.get(
                    download_url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=120
                )
                if resp.status_code >= 400:
                    logger.warning(f"[onedrive] 다운로드 실패: {name}")
                    total_scanned += 1
                    continue

                image_bytes = resp.content
                total_scanned += 1
                _sync_progress[shop_id]["total_scanned"] = total_scanned

                # 1차 필터링 (Blob 저장 전)
                passed, reason = _stage1_filter(image_bytes)
                if not passed:
                    logger.info(f"[onedrive] 1차 FAIL: {name} → {reason}")
                    continue

                # 1차 통과 → Blob 저장
                relative_path = _get_relative_path(photo)
                content_type = photo.get("file", {}).get("mimeType")
                if not content_type:
                    content_type, _ = mimetypes.guess_type(name)

                container_client.upload_blob(
                    name=relative_path,
                    data=image_bytes,
                    content_settings=ContentSettings(content_type=content_type),
                    overwrite=True
                )

                blob_url = (
                    f"https://stctrla.blob.core.windows.net/"
                    f"{container_name}/{quote(relative_path)}"
                )
                photo_id = (
                    f"photo_{shop_id}_"
                    f"{relative_path.replace('/', '_').replace(' ', '_')}"
                )

                # DB 저장
                save_photo(shop_id, {
                    "photo_id":      photo_id,
                    "blob_url":      blob_url,
                    "onedrive_url":  download_url,
                    "name":          name,
                    "last_modified": photo.get("lastModifiedDateTime", "")
                })

                photo_list_for_stage2.append({
                    "image_id": photo_id,
                    "blob_url": blob_url
                })

                stage1_passed += 1
                _sync_progress[shop_id]["stage1_passed"] = stage1_passed
                _sync_progress[shop_id]["message"] = (
                    f"분석 중... {total_scanned}/{len(photo_items)}장 "
                    f"(저장: {stage1_passed}장)"
                )
                logger.info(f"[onedrive] 1차 PASS → Blob 저장: {name}")

            except Exception as e:
                logger.error(f"[onedrive] 처리 실패 ({name}): {e}")

        logger.info(
            f"[onedrive] 1차 완료 → 스캔 {total_scanned} / Blob 저장 {stage1_passed}"
        )

        # Delta Link 저장
        if next_delta_link:
            try:
                from services.cosmos_db import update_shop_onedrive_info
                update_shop_onedrive_info(shop_id, {
                    "access_token": token,
                    "delta_link": next_delta_link
                })
                logger.info("[onedrive] Delta Link 저장 완료")
            except Exception as e:
                logger.error(f"[onedrive] Delta Link 저장 실패: {e}")

        # 2차 필터링 (GPT Vision)
        stage2_passed = 0
        if photo_list_for_stage2:
            try:
                from agents.photo_filter import run_photo_filter
                _sync_progress[shop_id]["message"] = (
                    f"AI 품질 분석 중... ({stage1_passed}장)"
                )
                filter_result = await run_photo_filter(shop_id, photo_list_for_stage2)
                stage2_passed = filter_result.get("stage2_passed", 0)
                logger.info(f"[onedrive] 2차 완료 → PASS {stage2_passed}/{stage1_passed}")
            except Exception as e:
                logger.error(f"[onedrive] 2차 필터링 실패 (무시): {e}")

        # 완료
        _sync_progress[shop_id].update({
            "status": "done",
            "total_scanned": total_scanned,
            "stage1_passed": stage1_passed,
            "stage2_passed": stage2_passed,
            "message": (
                f"완료! {total_scanned}장 스캔 → "
                f"저장 {stage1_passed}장 → "
                f"홍보용 {stage2_passed}장 선별"
            )
        })

        # 이메일 알림
        try:
            from services.cosmos_db import get_auth
            from services.email_service import send_draft_notification
            shop_auth = get_auth(shop_id) or {}
            owner_email = shop_auth.get("owner_email") or shop_auth.get("gmail")
            if owner_email:
                await send_draft_notification(
                    owner_email,
                    "사진 분석 완료",
                    f"OneDrive 사진 분석 완료!\n"
                    f"{total_scanned}장 스캔 → 홍보용 {stage2_passed}장 선별됐어요."
                )
        except Exception as e:
            logger.error(f"[onedrive] 완료 알림 실패 (무시): {e}")

    except Exception as e:
        traceback.print_exc()
        logger.error(f"[onedrive] 동기화 전체 실패: {e}")
        _sync_progress[shop_id].update({
            "status": "error",
            "message": f"오류: {str(e)}"
        })


# ── 엔드포인트

def _get_token_from_auth_me(request: Request) -> Optional[str]:
    """
    프론트엔드 EasyAuth /.auth/me 에서 액세스 토큰 조회.
    x-ms-token-aad-access-token 헤더가 없을 때 fallback으로 사용.
    쿠키를 그대로 전달해서 프론트엔드 세션에서 토큰을 가져옴.
    """
    try:
        frontend_url = os.getenv(
            "FRONTEND_URL",
            "https://bybaek-frontend-dcctbxfhdnhge4ap.koreacentral-01.azurewebsites.net"
        )
        auth_url = f"{frontend_url}/.auth/me"

        # 요청의 쿠키를 그대로 전달
        cookie_header = request.headers.get("cookie", "")
        headers = {}
        if cookie_header:
            headers["Cookie"] = cookie_header

        resp = requests.get(auth_url, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"[onedrive] /.auth/me 응답 {resp.status_code}")
            return None

        data = resp.json()
        if not data:
            return None

        # access_token 필드 직접 확인
        token = data[0].get("access_token")
        if token:
            logger.info("[onedrive] /.auth/me에서 토큰 획득 성공")
            return token

        # user_claims에서 찾기
        for claim in data[0].get("user_claims", []):
            if claim.get("typ") == "access_token":
                logger.info("[onedrive] user_claims에서 토큰 획득 성공")
                return claim.get("val")

        logger.warning("[onedrive] /.auth/me 응답에 토큰 없음")
    except Exception as e:
        logger.error(f"[onedrive] /.auth/me 조회 실패: {e}")
    return None


@router.post(
    "/sync-photos",
    response_model=SyncPhotosResponse,
    status_code=status.HTTP_200_OK,
)
async def sync_onedrive_photos(
    req: SyncPhotosRequest,
    request: Request,
    background_tasks: BackgroundTasks
) -> SyncPhotosResponse:
    """
    OneDrive 동기화 시작 (즉시 응답, 백그라운드 처리).
    프론트는 GET /onedrive/sync-status/{shop_id} 로 진행상황 폴링.
    """
    # 1. 헤더에서 토큰 조회
    token = request.headers.get("x-ms-token-aad-access-token")

    # 2. 없으면 /.auth/me로 fallback
    if not token:
        token = _get_token_from_auth_me(request)

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MS 로그인 필요. 토큰을 가져올 수 없어요."
        )

    shop_id = (
        request.headers.get("X-MS-CLIENT-PRINCIPAL-ID") or
        request.cookies.get("shop_id") or
        request.cookies.get("user_id") or
        "unknown"
    )

    if _sync_progress.get(shop_id, {}).get("status") == "running":
        return SyncPhotosResponse(
            success=False,
            message="이미 동기화가 진행 중이에요.",
            shop_id=shop_id
        )

    try:
        drive_id = get_user_drive_id(token)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OneDrive 연결 실패: {e}")

    background_tasks.add_task(
        _run_sync,
        shop_id=shop_id,
        token=token,
        drive_id=drive_id,
        force_full_sync=req.force_full_sync
    )

    logger.info(f"[onedrive] 동기화 시작 (백그라운드) → shop_id={shop_id}")

    return SyncPhotosResponse(
        success=True,
        message="사진 분석 시작! 완료되면 알림을 드릴게요.",
        shop_id=shop_id
    )


@router.get(
    "/sync-status/{shop_id}",
    response_model=SyncStatusResponse,
)
async def get_sync_status(shop_id: str) -> SyncStatusResponse:
    """
    동기화 진행상황 조회.
    프론트에서 5초 간격으로 폴링.
    """
    progress = _sync_progress.get(shop_id, {
        "status": "idle",
        "total_scanned": 0,
        "stage1_passed": 0,
        "stage2_passed": 0,
        "message": "동기화를 시작해주세요."
    })

    return SyncStatusResponse(
        shop_id=shop_id,
        status=progress["status"],
        total_scanned=progress["total_scanned"],
        stage1_passed=progress["stage1_passed"],
        stage2_passed=progress["stage2_passed"],
        message=progress["message"]
    )