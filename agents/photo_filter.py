"""
기능   : 바버샵 홍보 사진 1차(룰 기반) + 2차(GPT Vision) 필터링
입력   : shop_id, photo_list ([{"image_id": str, "blob_url": str}, ...])
출력   : {"total", "stage1_passed", "stage2_passed", "results", "failed"}
주요 흐름:
    1. run_photo_filter → run_stage1_filter → run_stage2_filter
    2. 1차: 밝기/흔들림만 체크 (바버샵 관련성 체크 제거 → Stage 2에 위임)
    3. 2차: GPT-4.1 Vision + Few-shot 평가 (25점 만점)
    4. 결과 CosmosDB 저장 (filter_status: passed/failed)

[수정 이력]
- _analyze_stage1: SAS URL 발급 후 다운로드 (403 방지)
- _save_pass_result: filter_status="passed" 추가
- _save_fail_result: filter_status="failed" 추가
- _generate_sas_url: AZURE_STORAGE_KEY → connection string에서 key 추출
- [FIX] _analyze_stage1: 바버샵 관련성 체크 제거 (뒷머리/측면 사진 탈락 방지)
- [FIX] _evaluate_photo: model_vibe를 instant_fail 대상에서 제외 (뒷면 사진 보호)
- [FIX] _save_pass_result: scores 필드 저장 추가 (photo_select에서 참조)
- [FIX] _generate_sas_url: BlobServiceClient 싱글톤 캐시로 성능 개선
- [v2] photo_category 분류 추가 (haircut_result/shop_atmosphere/barber_portrait/other_service/irrelevant)
- [v2] 카테고리별 instant_fail 제외 로직 분기 (shop_atmosphere/barber_portrait/other_service → gradient 제외)
- [v2] _save_pass_result: photo_category 필드 저장 추가 (photo_select 연동용)
- [v2.1] other_service 카테고리 추가 (미용실 스타일 시술 → gradient+model_vibe FAIL 제외, 기법 기준 분류)
"""

import os
import json
import asyncio
import random
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta

import cv2
import numpy as np
from openai import AsyncAzureOpenAI
from openai import (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
    APIStatusError,
)
from azure.storage.blob import BlobSasPermissions, generate_blob_sas, BlobServiceClient

# ── 설정값 ────────────────────────────────────────────────────────────────────

# 1차 기준 (밝기/흔들림만)
# 선명도: 명백한 흔들림(< STAGE1_LAPLACIAN_MIN)만 즉시 탈락시키고, 그 이상 경계 구간은
# Stage2 GPT Vision(sharpness 항목, instant_fail 대상)에 최종 판단을 위임한다.
# 40 → 10 하향: 선명하지만 고주파 엣지가 적은 사진(얕은 심도 인물·미니멀 배경 등)이
# 흔들림으로 오탈락하던 문제 해결. 진짜 흔들린 사진은 Stage2 sharpness가 잡는다.
STAGE1_LAPLACIAN_MIN  = 10     # 명백한 흔들림 즉시탈락 기준 (측정 전 해상도 정규화 후 적용)
STAGE1_BRIGHTNESS_MIN = 20     # 최소 밝기 (의도적 다크톤 브랜딩 컷 허용 위해 30→20 하향)
STAGE1_BRIGHTNESS_MAX = 240    # 최대 밝기

# [FIX] 선명도 측정 해상도 정규화
# 라플라시안 분산은 절대 해상도에 강하게 의존 → 저해상도 사진은 선명해도 값이 구조적으로 낮게 나옴
# (예: 43KB 헤어샘플 사진이 선명한데도 12.6으로 오탈락).
# 측정 직전 장변을 이 값으로 통일(작으면 확대·크면 축소)해 해상도 편향을 제거한다.
STAGE1_MEASURE_LONG_EDGE = 1024
# [FIX] STAGE1_SKIN_RATIO_MIN 제거 → 바버샵 관련성 체크 Stage 2에 위임

# 2차 기준
STAGE2_PASS_THRESHOLD = 15     # 25점 중 15점 이상 PASS
STAGE2_INSTANT_FAIL   = 1      # 한 항목이라도 1점 이하면 즉시 FAIL
# [FIX] model_vibe는 instant_fail 제외 (뒷면/측면 사진은 표정이 안 보임)
STAGE2_INSTANT_FAIL_EXCLUDE = {"model_vibe"}

MAX_CONCURRENT        = 5
MAX_GOOD_EXAMPLES     = 5
MAX_BAD_EXAMPLES      = 3

KST = timezone(timedelta(hours=9))

# [FIX] BlobServiceClient 싱글톤 캐시 (매 사진마다 새로 생성 방지)
_blob_service_client = None

def _get_blob_service_client() -> BlobServiceClient:
    global _blob_service_client
    if _blob_service_client is None:
        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        _blob_service_client = BlobServiceClient.from_connection_string(connection_string)
    return _blob_service_client


# ── 일시적 오류 재시도 / 오류 상태 (task #41) ─────────────────────────────────
# "판정하지 못한 것"(인프라 장애)과 "판정해서 탈락시킨 것"(콘텐츠)은 다른 상태다.
# 일시적 오류는 재시도하고, 소진 시에도 failed가 아니라 error 상태로 남긴다.


class TransientFilterError(Exception):
    """일시적 오류가 재시도까지 소진된 경우. 이 예외는 '판정 실패'(error)로 처리되며,
    콘텐츠 탈락(failed)과 구분된다."""
    pass

# 호출 단위 재시도: 첫 시도 포함 최대 3회, 지수 백오프 1s→2s→4s + 지터.
RETRY_MAX_ATTEMPTS   = 3
RETRY_BASE_DELAY_SEC = 1.0

# 사이클 단위 재시도 상한 (무한 재시도/비용 누수 방지).
# filter_attempts 가 이 값에 도달하면 더 이상 자동 재시도 대상이 아니다.
MAX_FILTER_ATTEMPTS = 3

# 일시적으로 취급할 오류 메시지 substring (예외 타입 분류의 보조 수단).
# 골든셋/프로덕션에서 실제 관측된 문자열 기준.
_TRANSIENT_SUBSTRINGS = (
    "failed to download image",   # Azure OpenAI 400: 이미지 다운로드 실패 (일시적)
    "unable to download",
    "timed out", "timeout",
    "connection", "temporarily unavailable", "service unavailable",
    "rate limit", "too many requests",
    "다운로드 실패", "이미지 읽기 실패",
)

# 재시도하면 안 되는(영구적) 오류 메시지 substring — 입력 자체의 문제.
# 이미지 크기 초과(2000px 등)는 task #30 영역이므로 재시도 대상에서 제외한다.
_PERMANENT_SUBSTRINGS = (
    "2000 pixels", "maximum allowed size", "image size", "too large",
    "invalid image", "unsupported image",
    "invalid api key", "unauthorized", "permission", "forbidden",
)


def _is_transient_error(exc: Exception) -> bool:
    """예외를 일시적(재시도 대상)/영구적으로 분류한다.

    분류 우선순위:
    1) 명백히 영구적인 예외 타입/메시지 → False
    2) 일시적 예외 타입(타임아웃/연결/레이트리밋/5xx/특정 4xx) → True
    3) 메시지 substring 보조 판정
    """
    msg = str(getattr(exc, "message", "") or exc).lower()

    # 1) 영구적 신호가 메시지에 있으면 재시도 안 함 (다운로드 실패 문구가 우선하지 않도록 먼저 체크)
    if any(s in msg for s in _PERMANENT_SUBSTRINGS):
        return False

    # 2) 예외 타입 기반 (신뢰도 높음)
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)):
        return True
    if isinstance(exc, APIStatusError):
        status_code = getattr(exc, "status_code", None)
        if status_code in (408, 409, 429, 500, 502, 503, 504):
            return True
        # 400 등 기타 status는 메시지로 최종 판단 (다운로드 실패=일시적 vs 형식오류=영구적)

    # 3) 메시지 substring 보조 판정
    if any(s in msg for s in _TRANSIENT_SUBSTRINGS):
        return True

    # 4) 네트워크성 표준 예외
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    return False


async def _retry_transient(fn, *, what: str, image_id: str = ""):
    """일시적 오류에 대해서만 지수 백오프(+지터) 재시도.

    fn: 매 시도마다 새로 호출되는 async 콜러블 (SAS URL 등을 시도마다 재생성하도록
        호출부에서 fn 내부에서 재발급할 것 — task #41 추가 1).
    영구적 오류는 즉시 raise. 일시적 오류는 소진 시 마지막 예외를 raise.
    """
    last_exc = None
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            transient = _is_transient_error(e)
            if not transient or attempt >= RETRY_MAX_ATTEMPTS:
                if not transient:
                    print(f"[photo_filter][ERROR] {what} 영구적 오류 → 재시도 안 함 ({image_id}): {e}")
                else:
                    print(f"[photo_filter][ERROR] {what} 재시도 {attempt}/{RETRY_MAX_ATTEMPTS} 소진 ({image_id}): {e}")
                raise
            delay = RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"[photo_filter][ERROR] {what} 일시적 오류 → {delay:.1f}s 후 재시도 {attempt+1}/{RETRY_MAX_ATTEMPTS} ({image_id}): {e}")
            await asyncio.sleep(delay)
    # 이론상 도달하지 않음
    if last_exc:
        raise last_exc


# ── 메인 진입점 ───────────────────────────────────────────────────────────────

async def run_photo_filter(shop_id: str, photo_list: list) -> dict:
    """
    1차 → 2차 통합 필터링 메인 함수.

    Args:
        shop_id:    샵 ID
        photo_list: [{"image_id": str, "blob_url": str, ...}, ...]

    Returns:
        {"total", "stage1_passed", "stage2_passed", "results", "failed"}
    """
    print(f"[photo_filter] 필터링 시작 -> shop_id={shop_id}, 대상={len(photo_list)}장")

    # [FIX] 이미 통과한 사진은 재필터링에서 제외
    already_passed = [p for p in photo_list if p.get("is_usable") is True and p.get("filter_status") == "passed"]
    photo_list = [p for p in photo_list if not (p.get("is_usable") is True and p.get("filter_status") == "passed")]
    if already_passed:
        print(f"[photo_filter] 이미 통과 {len(already_passed)}장 제외 → 대상 {len(photo_list)}장")

    if not photo_list:
        return {
            "total": len(already_passed),
            "stage1_passed": len(already_passed),
            "stage2_passed": len(already_passed),
            "errored": 0,
            "results": []
        }

    # STEP 1: 1차 필터링
    stage1_results   = await run_stage1_filter(photo_list)
    stage1_pass_list = [r for r in stage1_results if r["stage1_pass"]]
    # [task #41] 다운로드 등 일시적 오류(판정 불가)와 실제 콘텐츠 탈락을 분리
    stage1_error_list = [r for r in stage1_results if r.get("stage1_error")]
    stage1_fail_list  = [r for r in stage1_results if not r["stage1_pass"] and not r.get("stage1_error")]
    print(f"[photo_filter] 1차 완료 -> PASS {len(stage1_pass_list)} / FAIL {len(stage1_fail_list)} / ERROR {len(stage1_error_list)}")

    for photo in stage1_fail_list:
        await _save_fail_result(shop_id, photo, photo.get("stage1_reason", "stage1_fail"))

    stage1_errored = 0
    for photo in stage1_error_list:
        await _save_error_result(shop_id, photo, photo.get("stage1_reason", "stage1_error"))
        stage1_errored += 1

    if not stage1_pass_list:
        return {"total": len(photo_list), "stage1_passed": 0, "stage2_passed": 0,
                "errored": stage1_errored, "results": []}

    # STEP 2: 2차 필터링
    stage2_result = await run_stage2_filter(shop_id, stage1_pass_list)

    return {
        "total":         len(photo_list),
        "stage1_passed": len(stage1_pass_list),
        "stage2_passed": stage2_result["passed"],
        "errored":       stage1_errored + stage2_result.get("errored", 0),
        "results":       [r for r in stage2_result["results"] if r.get("stage2_pass")],
        "failed":        [r for r in stage2_result["results"] if not r.get("stage2_pass")]
    }


# ── 1차 필터링 (룰 기반) ──────────────────────────────────────────────────────

async def run_stage1_filter(photo_list: list) -> list:
    """1차 필터링: 밝기/흔들림만 체크. 바버샵 관련성은 Stage 2 GPT Vision에 위임."""
    results = []
    for photo in photo_list:
        image_id = photo.get("image_id", "")
        blob_url = photo.get("blob_url", "")

        # [task #41] 다운로드 등 일시적 오류로 판정 자체가 불가하면 stage1_fail 이 아니라
        # error 로 표시한다 ("판정 못한 것" ≠ "판정해서 탈락").
        try:
            pass_flag, reason = await _analyze_stage1(blob_url)
            stage1_error = False
        except TransientFilterError as e:
            print(f"[photo_filter][ERROR] Stage1 판정 불가 ({image_id}): {e}")
            pass_flag, reason = "Fail", str(e)
            stage1_error = True

        results.append({
            "image_id":      image_id,
            "blob_url":      blob_url,
            "stage1_pass":   pass_flag == "Pass",
            "stage1_reason": reason,
            "stage1_error":  stage1_error,
            **{k: v for k, v in photo.items() if k not in ("image_id", "blob_url")}
        })

    return results


def _normalize_for_sharpness(gray: "np.ndarray") -> "np.ndarray":
    """
    선명도(라플라시안 분산) 측정 전용 해상도 정규화.

    장변을 STAGE1_MEASURE_LONG_EDGE 로 통일한다(작으면 확대·크면 축소).
    라플라시안 분산은 픽셀 수에 의존하므로, 정규화하지 않으면 저해상도 사진은
    선명해도 값이 낮게 나와 오탈락한다. 축소는 INTER_AREA, 확대는 INTER_CUBIC 사용.

    이 정규화는 '측정용'이며 저장/업로드 이미지에는 영향을 주지 않는다.
    """
    h, w = gray.shape[:2]
    long_edge = max(h, w)
    if long_edge == STAGE1_MEASURE_LONG_EDGE:
        return gray
    scale = STAGE1_MEASURE_LONG_EDGE / long_edge
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(gray, new_size, interpolation=interp)


async def _analyze_stage1(blob_url: str) -> tuple:
    """
    blob URL → SAS 발급 → 임시 파일 다운로드 → 밝기/흔들림 체크.

    [FIX] 바버샵 관련성 체크 제거:
    - 뒷머리/측면 사진은 얼굴 미검출 + 피부 비중 낮아서 탈락하던 문제 해결
    - 관련성 판단은 Stage 2 GPT Vision에 위임

    Returns: ("Pass"|"Fail", reason_str)
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name

        async def _download():
            # [task #41 추가 1] SAS URL을 매 시도마다 재발급 (만료/무효 SAS 대비).
            sas_url = _generate_sas_url(blob_url)
            await asyncio.to_thread(urllib.request.urlretrieve, sas_url, tmp_path)

        try:
            await _retry_transient(_download, what="Stage1 다운로드")
        except Exception as e:
            # 다운로드 실패는 일시적 성격 → 판정 불가(error)로 승격. 콘텐츠 탈락 아님.
            raise TransientFilterError(f"다운로드 실패: {e}") from e
    except TransientFilterError:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise

    try:
        image = cv2.imread(tmp_path)
        if image is None:
            return "Fail", "이미지 읽기 실패"

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # 1) 흔들림 체크 (Laplacian variance)
        # [FIX] 측정 전 장변을 STAGE1_MEASURE_LONG_EDGE 로 정규화 → 해상도 편향 제거.
        #       (저해상도 선명 사진이 구조적으로 낮은 분산으로 오탈락하던 문제 해결)
        gray_norm = _normalize_for_sharpness(gray)
        laplacian_var = cv2.Laplacian(gray_norm, cv2.CV_64F).var()
        if laplacian_var < STAGE1_LAPLACIAN_MIN:
            return "Fail", f"초점 흐림 ({laplacian_var:.1f})"

        # 2) 밝기 체크 (원본 해상도 기준 — 밝기는 해상도 불변이므로 정규화 불필요)
        avg_brightness = np.mean(gray)
        if avg_brightness < STAGE1_BRIGHTNESS_MIN or avg_brightness > STAGE1_BRIGHTNESS_MAX:
            return "Fail", f"밝기 부적절 ({avg_brightness:.1f})"

        # [FIX] 바버샵 관련성 체크 제거 → Stage 2 GPT Vision에 위임
        # 선명도 경계 구간(STAGE1_LAPLACIAN_MIN 이상)도 Stage2 sharpness 평가에 위임
        return "Pass", f"1차 통과 (선명도:{laplacian_var:.0f}, 밝기:{avg_brightness:.0f})"

    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ── 2차 필터링 (GPT Vision) ───────────────────────────────────────────────────

async def run_stage2_filter(shop_id: str, stage1_pass_list: list) -> dict:
    """2차 필터링 메인 함수 (1차 PASS 사진만 받음)."""
    print(f"[photo_filter] 2차 필터링 시작 -> {len(stage1_pass_list)}장")

    reference_photos = await _load_reference_photos(shop_id)
    good_refs = [p for p in reference_photos if p.get("label") == "good"][:MAX_GOOD_EXAMPLES]
    bad_refs  = [p for p in reference_photos if p.get("label") == "bad"][:MAX_BAD_EXAMPLES]

    print(f"[photo_filter] Few-shot 레퍼런스 -> good {len(good_refs)}장 / bad {len(bad_refs)}장")
    if not good_refs:
        print("[photo_filter] 레퍼런스 없음 -> 기준만으로 평가 진행")

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def evaluate_with_limit(photo):
        async with semaphore:
            return await _evaluate_photo(
                image_id=photo["image_id"],
                blob_url=photo["blob_url"],
                good_refs=good_refs,
                bad_refs=bad_refs
            )

    tasks   = [evaluate_with_limit(p) for p in stage1_pass_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    passed, failed, errored = [], [], []
    for photo, result in zip(stage1_pass_list, results):
        # [task #41] 일시적 오류(재시도 소진)는 '판정 불가'(error)로 분리.
        # gather 예외가 TransientFilterError 이거나, _is_transient_error 로 판별되면 error.
        if isinstance(result, TransientFilterError) or (
            isinstance(result, Exception) and _is_transient_error(result)
        ):
            print(f"[photo_filter][ERROR] 2차 판정 불가 ({photo['image_id']}): {result}")
            try:
                await _save_error_result(shop_id, photo, str(result) or "evaluation_error")
            except Exception as e:
                print(f"[photo_filter][ERROR] ERROR 저장 실패 (건너뜀): {e}")
            errored.append(photo["image_id"])
            continue

        if isinstance(result, Exception):
            # 비일시적(영구) 예외 → 기존 동작 유지: 콘텐츠 탈락과 동일 처리
            print(f"[photo_filter] 평가 오류 ({photo['image_id']}): {result}")
            result = _make_fail_result(photo["image_id"], "evaluation_error")

        if result.get("stage2_pass"):
            passed.append(result)
            try:
                await _save_pass_result(shop_id, photo, result)
            except Exception as e:
                print(f"[photo_filter] DB 저장 실패 (건너뜀): {e}")
        else:
            failed.append(result)
            try:
                await _save_fail_result(shop_id, photo, result.get("reason", "stage2_fail"))
            except Exception as e:
                print(f"[photo_filter] FAIL 저장 실패 (건너뜀): {e}")

    print(f"[photo_filter] 2차 완료 -> PASS {len(passed)} / FAIL {len(failed)} / ERROR {len(errored)}")
    return {
        "total":   len(stage1_pass_list),
        "passed":  len(passed),
        "failed":  len(failed),
        "errored": len(errored),
        "results": passed + failed
    }


async def _evaluate_photo(
    image_id: str,
    blob_url: str,
    good_refs: list,
    bad_refs: list
) -> dict:
    """
    GPT-4.1 Vision + Few-shot 평가 (25점 만점).
    통과: 15점 이상 AND model_vibe 제외 항목 모두 2점 이상

    [FIX] model_vibe instant_fail 제외:
    - 뒷면/측면 사진은 표정이 안 보여서 model_vibe 0~1점 나올 수 있음
    - instant_fail 판정에서 model_vibe 제외하여 탈락 방지
    """
    print(f"[photo_filter] 2차 평가 중 -> {image_id}")

    api_key     = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY")
    endpoint    = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    deployment  = (
        os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )

    client = AsyncAzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version
    )

    async def _call_vision():
        # [task #41 추가 1] SAS URL을 매 시도마다 재발급한다.
        # 다운로드 실패의 원인이 만료/무효 SAS였을 경우 재시도가 무의미해지는 것을 방지.
        sas_url  = _generate_sas_url(blob_url)
        messages = _build_vision_prompt(sas_url, good_refs, bad_refs)
        return await client.chat.completions.create(
            model=deployment,
            messages=messages,
            max_completion_tokens=2000,
        )

    # 1) Vision 호출 — 일시적 오류만 재시도. 소진 시 TransientFilterError 로 승격.
    try:
        response = await _retry_transient(_call_vision, what="Vision 호출", image_id=image_id)
    except Exception as e:
        if _is_transient_error(e):
            # 재시도까지 소진된 일시적 오류 → '판정 불가'(error). 콘텐츠 탈락 아님.
            raise TransientFilterError(str(e)) from e
        # 영구적 오류(인증/형식/이미지 크기 등) → 기존 동작 유지(콘텐츠 탈락과 동일 처리)
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))

    # 2) 응답 파싱 + 채점 (파싱 실패는 영구적 → 기존 fail 처리 유지)
    try:
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        gpt_result = json.loads(raw)

        scores         = gpt_result.get("scores", {})
        total_score    = gpt_result.get("total", sum(scores.values()))
        photo_category = gpt_result.get("photo_category", "haircut_result")

        # [FIX] instant_fail 판정 — 카테고리별 제외 항목 분기
        exclude = set(STAGE2_INSTANT_FAIL_EXCLUDE)  # {"model_vibe"}
        if photo_category in ("shop_atmosphere", "barber_portrait", "other_service"):
            exclude.add("gradient")
        instant_fail = any(
            v <= STAGE2_INSTANT_FAIL
            for k, v in scores.items()
            if k not in exclude
        )

        # [1차 런칭 스코프] 바버샵 기법(페이드/언더컷 등) 결과만 통과.
        # other_service(롱헤어·펌·염색 등 미용실 스타일)는 irrelevant와 동일하게 즉시 탈락.
        # photo_category 값 자체는 그대로 저장되므로, 추후 샵별 "다운펌/염색 허용" 예외
        # 설정을 열 때 이 태그를 재활용할 수 있음.
        if photo_category in ("irrelevant", "other_service"):
            instant_fail = True

        stage2_pass  = (total_score >= STAGE2_PASS_THRESHOLD) and not instant_fail

        fade_cut_score = round(scores.get("gradient", 0) / 5, 2)
        angle          = _classify_angle(gpt_result.get("detected_angle", "unknown"))

        result = {
            "image_id":            image_id,
            "stage2_pass":         stage2_pass,
            "stage2_score":        round(total_score / 25, 2),
            "stage2_tags":         gpt_result.get("style_tags", []),
            "promo_effectiveness": round(total_score / 25, 2),
            "scores":              scores,
            "total_score":         total_score,
            "reason":              gpt_result.get("reason", ""),
            "fade_cut_score":      fade_cut_score,
            "detected_angle":      angle,
            "brightness":          _judge_brightness(scores.get("lighting", 3)),
            "sharpness":           "high" if scores.get("sharpness", 0) >= 3 else "low",
            "photo_category":      photo_category,
        }

        status = "PASS" if stage2_pass else "FAIL"
        print(f"[photo_filter] {status} {image_id} -> {total_score}/25점 (카테고리:{photo_category}, 페이드:{fade_cut_score}, 각도:{angle})")
        return result

    except Exception as e:
        # 응답 파싱/채점 단계 오류(모델이 잘못된 JSON 반환 등)는 영구적 성격 →
        # 기존 동작 유지(콘텐츠 탈락과 동일 처리). 일시적 오류는 위 Vision 호출 단계에서
        # 이미 TransientFilterError 로 분리되었다.
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))


# ── SAS URL 생성 ──────────────────────────────────────────────────────────────

def _generate_sas_url(blob_url: str, hours: int = 1) -> str:
    """
    순수 blob URL → SAS URL 발급.
    [FIX] BlobServiceClient 싱글톤 캐시 사용 (매 사진마다 재생성 방지)
    """
    blob_url = blob_url.split("?")[0]
    path     = blob_url.replace("https://bybaekstore1.blob.core.windows.net/", "")
    parts    = path.split("/", 1)
    container_name = parts[0]
    blob_name      = parts[1]

    client       = _get_blob_service_client()
    account_name = client.account_name
    account_key  = client.credential.account_key

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=container_name,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(hours=hours),
    )
    return f"{blob_url}?{sas_token}"


# ── 프롬프트 빌더 ─────────────────────────────────────────────────────────────

def _build_vision_prompt(blob_url: str, good_refs: list, bad_refs: list) -> list:
    """Few-shot 프롬프트 구성."""
    system_content = """너는 경력 20년의 바버샵 전문가이자 인스타그램 마케터야.
바버샵 홍보용 사진의 퀄리티를 전문가 기준으로 평가해줘.

[핵심 원칙 — 무엇보다 먼저 이해할 것]
BYBAEK 필터링의 유일한 판단 기준은 "사진에 바버샵 스타일 기법(페이드/언더컷/
스킨페이드/디자인라인/텍스처드크롭 등)이 명확히 보이는가"이다.
- 성별은 무관하다 (여성이어도 바버샵 스타일 기법이 보이면 통과).
- 사진의 출처는 판단 기준이 아니다. 실제 손님을 찍은 것이든, 인터넷에서 가져온
  레퍼런스/스톡 사진이든, 연예인 사진 캡처든 전부 허용된다.
- 매장 맥락(매장 배경/유니폼/시술 도구/이발 장면)의 유무도 판단 기준이 아니다.
  스튜디오 무지 배경이어도 바버 기법이 보이면 통과다.

[1단계: 사진 카테고리 분류 - 반드시 먼저 판단]
- "haircut_result"  : 바버샵 스타일 헤어컷 사진 (페이드/언더컷/스킨페이드/디자인라인/
                      텍스처드크롭 등 바버 기법이 명확히 보임 — 성별·출처·매장맥락 무관)
- "shop_atmosphere" : 매장 인테리어/분위기가 주가 되는 사진
- "barber_portrait" : 인물이 주가 되면서 바버샵 관련 헤어 스타일링이 함께 보이는 사진
                      (판단은 매장 맥락 유무가 아니라 바버 헤어 기법이 보이는지로 한다)
- "other_service"   : 이 샵과 관련은 있지만 바버샵 스타일이 아닌 헤어 시술 사진
                      (미용실 스타일 펌/레이어드컷/염색 등 — 성별이 아니라 기법으로 판단)
- "irrelevant"      : 위 넷 다 아닌 사진. 아래에 하나라도 해당하면 irrelevant로 분류:
                      • 바버샵 스타일 기법이 전혀 안 보임: 사람이 주제가 아니거나,
                        옷/액세서리/몸/포즈가 주제라서 헤어 기법을 판단할 수 없는 경우
                      • 실사 사진이 아닌 것: 일러스트/인포그래픽/헤어스타일 차트/그림/합성
                      • 앱 화면 스크린샷: 상태바(시계·배터리)·앱 버튼·타임라인·재생바·
                        "취소/완료/비디오/조절/필터/자르기" 등 UI 텍스트, 인스타/틱톡/Siri 등
                        앱 인터페이스가 조금이라도 보이면 실제 헤어가 찍혀 있어도 irrelevant
                      • 풍경/음식/기타 무관 사진
                      ※ "스톡/모델/레퍼런스 사진"이라는 이유만으로는 절대 irrelevant가
                        아니다. 출처는 기준이 아니며, 바버 기법이 보이면 통과시킨다.

[2단계: 카테고리별 평가 기준 - 각 5점, 총 25점]
1. gradient  : 페이드 그라데이션이 자연스럽고 경계가 뭉치지 않을 것
2. lighting  : 너무 어둡거나 과노출되지 않고 자연스러울 것
3. background: 복잡하거나 지저분하지 않고 깔끔할 것
4. model_vibe: 모델 표정/분위기가 홍보용으로 적합할 것
              ※ 뒷면/측면 사진은 표정이 안 보이므로 자세/헤어스타일 완성도로 평가
5. sharpness : 핀트가 맞고 구도가 홍보용으로 적합할 것

[카테고리별 평가 규칙]
● haircut_result → 5개 항목 모두 정상 평가
● shop_atmosphere / barber_portrait → gradient는 평가 대상 아님, 0으로 고정.
  lighting/background/model_vibe/sharpness 위주로 평가 (gradient=0이어도 FAIL 아님)
● other_service → gradient 평가 대상 아님(0 고정), lighting/background/sharpness 위주로 평가.
  model_vibe와 gradient 둘 다 즉시 FAIL 대상에서 제외 (미용실 스타일 시술 예외 동작 복원)
● irrelevant → 모든 항목 0점, 즉시 FAIL

[분류 우선순위 — 무엇보다 먼저 적용]
아래 중 하나라도 해당하면 헤어가 잘 보여도 반드시 "irrelevant"로 분류한다
(haircut_result/barber_portrait로 절대 분류 금지):
1) 실사 사진이 아님 (일러스트·인포그래픽·헤어스타일 차트·그림·합성)
2) 앱 UI 스크린샷 (상태바·앱 버튼·영상 타임라인/재생바·인스타/틱톡/Siri 화면·
   "취소/완료/비디오/조절/필터/자르기" 등 인터페이스 텍스트가 보임)
3) 바버샵 스타일 기법이 전혀 안 보임 (사람이 주제가 아니거나, 옷/액세서리/몸/포즈가
   주제라서 헤어 기법을 판단할 수 없는 경우)
반대로, 사진이 "스톡/모델/레퍼런스/연예인 캡처"라는 이유만으로는 절대 배제하지 않는다.
출처는 기준이 아니며 매장 맥락(배경/유니폼/도구) 유무도 기준이 아니다 — 오직 바버 기법이
보이는지로 판단한다. barber_portrait도 매장 맥락이 아니라 바버 헤어 기법이 보이는지로
판단한다.

[통과 기준]
- 총점 25점 기준 15점 이상 PASS
- haircut_result: gradient/lighting/background/sharpness 중 하나라도 1점 이하면 즉시 FAIL
- shop_atmosphere/barber_portrait: lighting/background/sharpness 중 하나라도 1점 이하면 즉시 FAIL
  (gradient는 제외, model_vibe도 제외)
- other_service: lighting/background/sharpness 중 하나라도 1점 이하면 즉시 FAIL
  (gradient는 제외, model_vibe도 제외)
- model_vibe는 즉시 FAIL 대상 제외 (뒷면 사진 보호)

[각도 감지]
- "back_side": 뒷면 또는 측면 (페이드 그라데이션 중심)
- "front"    : 정면 (스타일링 중심)
- "unknown"  : 판단 불가

[응답 형식] JSON으로만:
{
  "photo_category": "haircut_result" | "shop_atmosphere" | "barber_portrait" | "other_service" | "irrelevant",
  "scores": {
    "gradient": 0~5,
    "lighting": 0~5,
    "background": 0~5,
    "model_vibe": 0~5,
    "sharpness": 0~5
  },
  "total": 0~25,
  "detected_angle": "back_side" | "front" | "unknown",
  "style_tags": ["fade_cut", "side_part" 등],
  "reason": "평가 이유 1줄"
}"""

    messages = [{"role": "system", "content": system_content}]

    if good_refs:
        good_content = []
        for i, ref in enumerate(good_refs[:MAX_GOOD_EXAMPLES], 1):
            ref_url    = ref.get("blob_url", "")
            ref_reason = ref.get("reason", "원장님이 선택한 좋은 예시")
            if ref_url:
                good_content.append({"type": "text", "text": f"[좋은 예시 {i}] {ref_reason}"})
                good_content.append({"type": "image_url", "image_url": {"url": _generate_sas_url(ref_url), "detail": "low"}})
        if good_content:
            messages.append({"role": "user", "content": good_content})

    if bad_refs:
        bad_content = []
        for i, ref in enumerate(bad_refs[:MAX_BAD_EXAMPLES], 1):
            ref_url    = ref.get("blob_url", "")
            ref_reason = ref.get("reason", "원장님이 탈락시킨 나쁜 예시")
            if ref_url:
                bad_content.append({"type": "text", "text": f"[나쁜 예시 {i}] {ref_reason}"})
                bad_content.append({"type": "image_url", "image_url": {"url": _generate_sas_url(ref_url), "detail": "low"}})
        if bad_content:
            messages.append({"role": "user", "content": bad_content})

    if good_refs or bad_refs:
        messages.append({
            "role": "assistant",
            "content": "네, 원장님 기준을 이해했습니다. 평가할 사진을 보여주세요."
        })

    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": blob_url, "detail": "high"}},
            {"type": "text", "text": "이 사진을 원장님 기준에 따라 채점하고 JSON으로만 응답해줘."}
        ]
    })

    return messages


# ── DB 저장 ───────────────────────────────────────────────────────────────────

async def _save_pass_result(shop_id: str, photo: dict, result: dict):
    """2차 PASS 결과 CosmosDB 저장."""
    from services.cosmos_db import save_photo_meta
    try:
        now_kst = datetime.now(KST).isoformat()
        doc = {
            "id":             photo["image_id"],
            "shop_id":        shop_id,
            "blob_url":       photo["blob_url"].split("?")[0],
            "stage1_pass":    True,
            "stage2_pass":    True,
            "stage2_tags":    result.get("stage2_tags", []),
            "total_score":    result["total_score"],
            "fade_cut_score": result["fade_cut_score"],
            "detected_angle": result["detected_angle"],
            "scores":         result.get("scores", {}),   # [FIX] photo_select에서 참조
            "photo_category": result.get("photo_category", "haircut_result"),
            "is_usable":      True,
            "filter_status":  "passed",
            "analyzed_at":    now_kst
        }
        save_photo_meta(shop_id, doc)
        print(f"[photo_filter] DB 저장 완료 -> {photo['image_id']}")
    except Exception as e:
        print(f"[photo_filter] DB 저장 오류: {e}")


async def _save_fail_result(shop_id: str, photo: dict, reason: str = "stage2_fail"):
    """2차 FAIL 결과 CosmosDB 저장 (is_usable=False)."""
    # [FIX] 이미 통과한 사진은 FAIL로 덮어쓰지 않음
    try:
        from services.cosmos_db import get_photo_by_id
        existing = get_photo_by_id(shop_id, photo["image_id"])
        if existing and existing.get("is_usable") is True:
            print(f"[photo_filter] 통과 사진 보호 → FAIL 저장 건너뜀: {photo['image_id']}")
            return
    except Exception:
        pass

    from services.cosmos_db import save_photo_meta
    try:
        now_kst = datetime.now(KST).isoformat()
        doc = {
            "id":            photo["image_id"],
            "shop_id":       shop_id,
            "blob_url":      photo["blob_url"].split("?")[0],
            "stage1_pass":   False if "stage1" in reason else True,
            "stage2_pass":   False,
            "is_usable":     False,
            "filter_status": "failed",
            "analyzed_at":   now_kst,
            "fail_reason":   reason
        }
        save_photo_meta(shop_id, doc)
    except Exception as e:
        print(f"[photo_filter] FAIL 저장 오류 (건너뜀): {e}")


async def _save_error_result(shop_id: str, photo: dict, error_reason: str = "evaluation_error"):
    """[task #41] 일시적 오류로 '판정 불가'한 사진을 error 상태로 저장.

    콘텐츠 탈락(_save_fail_result)과 다른 상태:
    - is_usable = None       (False 아님 → /photos/all 에서 '탈락'으로 낙인되지 않음)
    - filter_status = "error"
    - error_reason = 분류된 사유
    - filter_attempts += 1   (사이클 단위 재시도 상한 MAX_FILTER_ATTEMPTS 용)

    기존 통과 사진은 절대 덮어쓰지 않는다.
    """
    from services.cosmos_db import get_photo_by_id
    prev_attempts = 0
    try:
        existing = get_photo_by_id(shop_id, photo["image_id"])
        if existing:
            if existing.get("is_usable") is True:
                print(f"[photo_filter] 통과 사진 보호 → ERROR 저장 건너뜀: {photo['image_id']}")
                return
            prev_attempts = int(existing.get("filter_attempts") or 0)
    except Exception:
        pass

    from services.cosmos_db import save_photo_meta
    try:
        now_kst = datetime.now(KST).isoformat()
        doc = {
            "id":              photo["image_id"],
            "shop_id":         shop_id,
            "blob_url":        photo["blob_url"].split("?")[0],
            # is_usable/stage 값은 판정하지 못했으므로 확정하지 않는다(None → 기존값 유지).
            "is_usable":       None,
            "filter_status":   "error",
            "error_reason":    error_reason,
            "filter_attempts": prev_attempts + 1,
            "analyzed_at":     now_kst,
        }
        save_photo_meta(shop_id, doc)
        print(f"[photo_filter][ERROR] error 상태 저장 -> {photo['image_id']} (attempts={prev_attempts + 1})")
    except Exception as e:
        print(f"[photo_filter][ERROR] ERROR 저장 오류 (건너뜀): {e}")


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _classify_angle(detected: str) -> str:
    angle_map = {
        "back_side": "back_side",
        "back":      "back_side",
        "side":      "back_side",
        "back-side": "back_side",
        "front":     "front",
        "unknown":   "unknown"
    }
    return angle_map.get(str(detected).lower(), "unknown")


def _judge_brightness(score: int) -> str:
    return "good" if score >= 4 else "dark" if score >= 2 else "bright"


def _make_fail_result(image_id: str, reason: str) -> dict:
    return {
        "image_id":       image_id,
        "stage2_pass":    False,
        "reason":         reason,
        "total_score":    0,
        "detected_angle": "unknown",
        "fade_cut_score": 0.0
    }


async def _load_reference_photos(shop_id: str) -> list:
    """
    Few-shot 레퍼런스 사진 로드.
    레퍼런스 앨범 없으면 빈 리스트 반환 → 기준만으로 동작.
    """
    try:
        from services.cosmos_db import get_album, get_photo_by_id
        album_id = f"reference_{shop_id}"
        album    = get_album(shop_id, album_id)

        if not album:
            print(f"[photo_filter] 레퍼런스 앨범 없음 ({album_id}) -> Few-shot 없이 진행")
            return []

        photo_ids  = album.get("photo_ids", [])
        references = []
        for item in photo_ids:
            photo_id = item if isinstance(item, str) else item.get("photo_id") or item.get("id")
            if not photo_id:
                continue
            photo = get_photo_by_id(shop_id, photo_id)
            if photo:
                references.append(photo)

        print(f"[photo_filter] 레퍼런스 {len(references)}장 로드 완료")
        return references

    except Exception as e:
        print(f"[photo_filter] 레퍼런스 로드 실패: {e}")
        return []