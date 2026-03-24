import os
import json
import asyncio
import tempfile
import urllib.request
from datetime import datetime, timezone, timedelta

import cv2
import numpy as np
from openai import AsyncAzureOpenAI

# [설정값]

# 1차 기준
STAGE1_LAPLACIAN_MIN  = 40    # 흔들림 기준 완화 (스마트폰 사진 통과)
STAGE1_BRIGHTNESS_MIN = 30    # 최소 밝기 완화
STAGE1_BRIGHTNESS_MAX = 240   # 최대 밝기 완화
STAGE1_SKIN_RATIO_MIN = 2.0   # 뒷머리/측면 사진 통과



# 2차 기준
STAGE2_PASS_THRESHOLD = 15      # 25점 중 15점 이상 PASS
STAGE2_INSTANT_FAIL   = 1       # 한 항목이라도 1점 이하면 즉시 FAIL
MAX_CONCURRENT        = 5
MAX_GOOD_EXAMPLES     = 5
MAX_BAD_EXAMPLES      = 3

KST = timezone(timedelta(hours=9))

# [메인] 1차 + 2차 통합 진입점
async def run_photo_filter(
    shop_id: str,
    photo_list: list
) -> dict:
    """
    1차 -> 2차 통합 필터링 메인 함수.

    Args:
        shop_id:    샵 ID
        photo_list: [{"image_id": str, "blob_url": str, ...}, ...]

    Returns:
        {
          "total": int,
          "stage1_passed": int,
          "stage2_passed": int,
          "results": [...]   # 2차 PASS 결과만
        }
    """
    print(f"[photo_filter] 필터링 시작 -> shop_id={shop_id}, 대상={len(photo_list)}장")

    if not photo_list:
        return {"total": 0, "stage1_passed": 0, "stage2_passed": 0, "results": []}

    # STEP 1: 1차 필터링
    stage1_results   = await run_stage1_filter(photo_list)
    stage1_pass_list = [r for r in stage1_results if r["stage1_pass"]]
    print(f"[photo_filter] 1차 완료 -> PASS {len(stage1_pass_list)} / FAIL {len(stage1_results) - len(stage1_pass_list)}")

    if not stage1_pass_list:
        return {"total": len(photo_list), "stage1_passed": 0, "stage2_passed": 0, "results": []}

    # STEP 2: 2차 필터링
    stage2_result = await run_stage2_filter(shop_id, stage1_pass_list)

    return {
        "total":         len(photo_list),
        "stage1_passed": len(stage1_pass_list),
        "stage2_passed": stage2_result["passed"],
        "results":       [r for r in stage2_result["results"] if r.get("stage2_pass")]
    }


# [1차 필터링] 룰 기반 
async def run_stage1_filter(photo_list: list) -> list:
    """
    1차 필터링: 해상도/밝기/흔들림/바버샵 관련성 체크.
    DB쪽 코드 기반, blob URL 지원으로 확장.

    Returns:
        [{"image_id": str, "blob_url": str, "stage1_pass": bool, "stage1_reason": str}, ...]
    """
    results = []
    for photo in photo_list:
        image_id = photo.get("image_id", "")
        blob_url = photo.get("blob_url", "")

        pass_flag, reason = await _analyze_stage1(blob_url)

        results.append({
            "image_id":      image_id,
            "blob_url":      blob_url,
            "stage1_pass":   pass_flag == "Pass",
            "stage1_reason": reason,
            **{k: v for k, v in photo.items() if k not in ("image_id", "blob_url")}
        })

    return results

# 실데이터로 연동해야함!!!!!!!!
async def _analyze_stage1(blob_url: str) -> tuple:
    """
    blob URL -> 임시 파일 다운로드 -> 룰 기반 분석.
    지연님의 analyze_image_v2 로직 기반.

    Returns: ("Pass"|"Fail", reason_str)
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        urllib.request.urlretrieve(blob_url, tmp_path)
    except Exception as e:
        return "Fail", f"다운로드 실패: {e}"

    try:
        image = cv2.imread(tmp_path)
        if image is None:
            return "Fail", "이미지 읽기 실패"

        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # 1) 흔들림 체크 (Laplacian variance)
        laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
        if laplacian_var < STAGE1_LAPLACIAN_MIN:
            return "Fail", f"초점 흐림 ({laplacian_var:.1f})"

        # 2) 밝기 체크
        avg_brightness = np.mean(gray)
        if avg_brightness < STAGE1_BRIGHTNESS_MIN or avg_brightness > STAGE1_BRIGHTNESS_MAX:
            return "Fail", f"밝기 부적절 ({avg_brightness:.1f})"

        # 3) 바버샵 관련성 체크 (얼굴 인식 + 피부색 비중)
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        faces = face_cascade.detectMultiScale(gray, 1.3, 5)

        hsv        = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        lower_skin = np.array([0,  20,  70],  dtype=np.uint8)
        upper_skin = np.array([25, 255, 255], dtype=np.uint8)
        mask       = cv2.inRange(hsv, lower_skin, upper_skin)
        skin_ratio = (cv2.countNonZero(mask) / (height * width)) * 100

        if len(faces) == 0 and skin_ratio < STAGE1_SKIN_RATIO_MIN:
            return "Fail", f"관련성 낮음 (얼굴 미검출, 피부 비중 {skin_ratio:.1f}%)"

        return "Pass", f"1차 통과 (선명도:{laplacian_var:.0f}, 밝기:{avg_brightness:.0f}, 피부:{skin_ratio:.1f}%)"

    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# [2차 필터링] GPT-4.1 Vision + Few-shot
async def run_stage2_filter(
    shop_id: str,
    stage1_pass_list: list
) -> dict:
    """2차 필터링 메인 함수 (1차 PASS 사진만 받음)"""
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

    passed, failed = [], []
    for photo, result in zip(stage1_pass_list, results):
        if isinstance(result, Exception):
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

    print(f"[photo_filter] 2차 완료 -> PASS {len(passed)} / FAIL {len(failed)}")
    return {
        "total":   len(stage1_pass_list),
        "passed":  len(passed),
        "failed":  len(failed),
        "results": passed + failed
    }


async def _evaluate_photo(
    image_id: str,
    blob_url: str,
    good_refs: list,
    bad_refs: list
) -> dict:
    """
    GPT-4.1 Vision + Few-shot 평가.

    평가 항목 (각 5점, 총 25점):
      gradient   : 페이드 그라데이션 자연스러움
      lighting   : 조명 적절성
      background : 배경 깔끔함
      model_vibe : 모델 분위기/표정
      sharpness  : 핀트 + 구도

    통과: 15점 이상 AND 모든 항목 2점 이상
    """
    print(f"[photo_filter] 2차 평가 중 -> {image_id}")

    messages = _build_vision_prompt(blob_url, good_refs, bad_refs)

    api_key     = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY")
    endpoint    = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    deployment  = (
        os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )

    client = AsyncAzureOpenAI(
        api_key=api_key,
        azure_endpoint=endpoint,
        api_version=api_version
    )

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=messages,
            max_tokens=500,
            temperature=0
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        gpt_result = json.loads(raw)

        scores      = gpt_result.get("scores", {})
        total_score = gpt_result.get("total", sum(scores.values()))

        # 즉시 FAIL: 한 항목이라도 1점 이하
        instant_fail = any(v <= STAGE2_INSTANT_FAIL for v in scores.values())
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
            "sharpness":           "high" if scores.get("sharpness", 0) >= 3 else "low"
        }

        status = "PASS" if stage2_pass else "FAIL"
        print(f"[photo_filter] {status} {image_id} -> {total_score}/25점 (페이드:{fade_cut_score}, 각도:{angle})")
        return result

    except Exception as e:
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))


def _build_vision_prompt(blob_url: str, good_refs: list, bad_refs: list) -> list:
    """
    Few-shot 프롬프트 구성.

    구조:
      system    : 전문가 역할 + 5항목 평가 기준
      user      : 좋은 예시 사진들 (good_refs, detail: low)
      user      : 나쁜 예시 사진들 (bad_refs, detail: low)
      assistant : "기준 이해, 평가 준비됨"
      user      : 평가 대상 사진 (detail: high)
    """
    system_content = """너는 경력 20년의 바버샵 전문가이자 인스타그램 마케터야.
바버샵 홍보용 사진의 퀄리티를 전문가 기준으로 평가해줘.

[평가 기준 - 각 5점, 총 25점]
1. gradient  : 페이드 그라데이션이 자연스럽고 경계가 뭉치지 않을 것
2. lighting  : 너무 어둡거나 과노출되지 않고 자연스러울 것
3. background: 복잡하거나 지저분하지 않고 깔끔할 것
4. model_vibe: 모델 표정이 자연스럽고 홍보용으로 적합할 것
5. sharpness : 핀트가 맞고 구도가 홍보용으로 적합할 것

[통과 기준]
- 총점 25점 기준 15점 이상 PASS
- 어느 한 항목이라도 1점 이하면 즉시 FAIL (치명적 결함)

[각도 감지]
- "back_side": 뒷면 또는 측면 (페이드 그라데이션 중심)
- "front"    : 정면 (스타일링 중심)
- "unknown"  : 판단 불가

[응답 형식] JSON으로만:
{
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

    # Few-shot: 좋은 예시
    if good_refs:
        good_content = []
        for i, ref in enumerate(good_refs[:MAX_GOOD_EXAMPLES], 1):
            ref_url    = ref.get("blob_url", "")
            ref_reason = ref.get("reason", "원장님이 선택한 좋은 예시")
            if ref_url:
                good_content.append({"type": "text", "text": f"[좋은 예시 {i}] {ref_reason}"})
                good_content.append({"type": "image_url", "image_url": {"url": ref_url, "detail": "low"}})
        if good_content:
            messages.append({"role": "user", "content": good_content})

    # Few-shot: 나쁜 예시
    if bad_refs:
        bad_content = []
        for i, ref in enumerate(bad_refs[:MAX_BAD_EXAMPLES], 1):
            ref_url    = ref.get("blob_url", "")
            ref_reason = ref.get("reason", "원장님이 탈락시킨 나쁜 예시")
            if ref_url:
                bad_content.append({"type": "text", "text": f"[나쁜 예시 {i}] {ref_reason}"})
                bad_content.append({"type": "image_url", "image_url": {"url": ref_url, "detail": "low"}})
        if bad_content:
            messages.append({"role": "user", "content": bad_content})

    # 예시가 있으면 어시스턴트 확인 응답 삽입
    if good_refs or bad_refs:
        messages.append({
            "role": "assistant",
            "content": "네, 원장님 기준을 이해했습니다. 평가할 사진을 보여주세요."
        })

    # 평가 대상 사진
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": blob_url, "detail": "high"}},
            {"type": "text", "text": "이 사진을 원장님 기준에 따라 채점하고 JSON으로만 응답해줘."}
        ]
    })

    return messages

# [헬퍼]
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


async def _save_pass_result(shop_id: str, photo: dict, result: dict):
    """AG-010: 2차 PASS 결과 CosmosDB 저장 (실제 연결)"""
    from services.cosmos_db import save_photo_meta
    try:
        now_kst = datetime.now(KST).isoformat()
        doc = {
            "id":             photo["image_id"],
            "shop_id":        shop_id,
            "blob_url":       photo["blob_url"],
            "stage1_pass":    True,
            "stage2_pass":    True,
            "stage2_tags":    result.get("stage2_tags", []),
            "total_score":    result["total_score"],
            "fade_cut_score": result["fade_cut_score"],
            "detected_angle": result["detected_angle"],
            "is_usable":      True,
            "analyzed_at":    now_kst
        }
        save_photo_meta(shop_id, doc)
        print(f"[photo_filter] DB 저장 완료 -> {photo['image_id']}")
    except Exception as e:
        print(f"[photo_filter] DB 저장 오류: {e}")


async def _save_fail_result(shop_id: str, photo: dict, reason: str = "stage2_fail"):
    """AG-010: 2차 FAIL 결과 CosmosDB 저장 (is_usable=False)"""
    from services.cosmos_db import save_photo_meta
    try:
        now_kst = datetime.now(KST).isoformat()
        doc = {
            "id":             photo["image_id"],
            "shop_id":        shop_id,
            "blob_url":       photo["blob_url"],
            "stage1_pass":    True,
            "stage2_pass":    False,
            "is_usable":      False,
            "analyzed_at":    now_kst,
            "fail_reason":    reason
        }
        save_photo_meta(shop_id, doc)
    except Exception as e:
        print(f"[photo_filter] FAIL 저장 오류 (건너뜀): {e}")


async def _load_reference_photos(shop_id: str) -> list:
    """
    Few-shot 레퍼런스 사진 로드.

    라벨링 세션 완료 후 CosmosDB reference 앨범에서 가져옴.
    라벨링 전: 빈 리스트 반환 -> 기준만으로 동작.
    """
    try:
        from services.cosmos_db import get_album, get_photo_by_id
        album_id = f"reference_{shop_id}"
        album = get_album(shop_id, album_id)

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