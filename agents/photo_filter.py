"""
photo_filter.py - 2차 필터링 (AI Vision 평가)

22년 경력 바버샵 원장님 사진 선택 기준 반영:
1. 페이드 그라데이션 (뒷면/측면) 최우선
   - 한국인 두상 울퉁불퉁 → 정교한 샷 필요
2. 스타일링 (앞모습)
3. 분위기 (손님 포즈)
"""

import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from openai import AsyncAzureOpenAI

# [설정값]
STAGE2_PASS_THRESHOLD = 18      # 30점 중 18점 이상 PASS
MAX_CONCURRENT = 5
MAX_GOOD_EXAMPLES = 5           
MAX_BAD_EXAMPLES  = 3         


async def run_stage2_filter(
    shop_id: str,
    stage1_pass_list: list
) -> dict:
    """2차 필터링 메인 함수"""
    print(f"[photo_filter] 2차 필터링 시작 → shop_id={shop_id}, 대상={len(stage1_pass_list)}장")

    if not stage1_pass_list:
        print("[photo_filter] 대상 사진 없음 → 종료")
        return {"total": 0, "passed": 0, "failed": 0, "results": []}

    # STEP 1: 레퍼런스 로드
    reference_photos = await _load_reference_photos(shop_id)
    good_refs = [p for p in reference_photos if p.get("label") == "good"][:MAX_GOOD_EXAMPLES]
    bad_refs  = [p for p in reference_photos if p.get("label") == "bad"][:MAX_BAD_EXAMPLES]

    print(f"[photo_filter] 레퍼런스 → 좋은 예시 {len(good_refs)}장, 나쁜 예시 {len(bad_refs)}장")
    
    if not good_refs:
        print("[photo_filter] ⚠️ 레퍼런스 사진 없음 → 기본 평가 진행")

    # STEP 2: 병렬 처리
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def evaluate_with_limit(photo):
        async with semaphore:
            return await _evaluate_photo(
                image_id=photo["image_id"],
                blob_url=photo["blob_url"],
                stage1_data=photo,
                good_refs=good_refs,
                bad_refs=bad_refs
            )

    tasks = [evaluate_with_limit(photo) for photo in stage1_pass_list]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # STEP 3: 결과 처리
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
                print(f"[photo_filter] DB 저장 실패(건너뜀): {e}")
        else:
            failed.append(result)
            await _handle_fail_result(shop_id, photo, result)

    print(f"[photo_filter] 완료 → PASS {len(passed)}장 / FAIL {len(failed)}장")
    return {
        "total":   len(stage1_pass_list),
        "passed":  len(passed),
        "failed":  len(failed),
        "results": passed + failed
    }


async def _evaluate_photo(
    image_id: str,
    blob_url: str,
    stage1_data: dict,
    good_refs: list,
    bad_refs: list
) -> dict:
    """
    GPT Vision 평가 - 22년 경력 원장님 기준
    
    평가 기준:
    1. fade_gradient_clarity (8점): 페이드 그라데이션 선명도
       - 뒷면/측면 각도 우대
       - 한국인 두상 울퉁불퉁 → 굴곡 적고 자연스러운가
       
    2. styling_appeal (6점): 앞모습 스타일링 매력
       - 앞모습 각도 우대
       
    3. model_vibe (6점): 손님 포즈/분위기
       - 가게 분위기 어울림
       
    4. lighting (5점): 조명 품질
    5. background (5점): 배경 깔끔함
    
    합격: 18/30 이상
    """
    print(f"[photo_filter] 평가 중 → {image_id}")

    messages = _build_vision_prompt(blob_url, good_refs, bad_refs)

    api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    
    deployment = os.getenv("AZURE_OPENAI_VISION_DEPLOYMENT") or \
                 os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT") or \
                 os.getenv("AZURE_OPENAI_DEPLOYMENT")

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

        # 결과 가공
        total_score   = gpt_result.get("total", 0)
        promo_effectiveness = round(total_score / 30, 2)  # 0~1
        stage2_pass   = total_score >= STAGE2_PASS_THRESHOLD

        scores = gpt_result.get("scores", {})
        
        # 페이드컷 점수 (fade_gradient_clarity 기반)
        fade_cut_score = round(scores.get("fade_gradient_clarity", 0) / 8, 2)

        # 각도 분류 (photo_select에서 사용)
        angle = _classify_angle(gpt_result.get("detected_angle", "unknown"))

        result = {
            "image_id":       image_id,
            "stage2_pass":    stage2_pass,
            "stage2_score":   promo_effectiveness,
            "stage2_tags":    gpt_result.get("style_tags", []),
            "promo_effectiveness": promo_effectiveness,
            "scores":         scores,
            "total_score":    total_score,
            "reason":         gpt_result.get("reason", ""),
            "fade_cut_score": fade_cut_score,
            "detected_angle": angle,  # "back_side" | "front" | "unknown"
            "brightness":     _judge_brightness(scores.get("lighting", 3)),
            "sharpness":      "high"  # Vision 분석 통과했으면 선명도 보장
        }

        status = "✅ PASS" if stage2_pass else "❌ FAIL"
        print(f"[photo_filter] {status} {image_id} → {total_score}/30점 (페이드:{fade_cut_score}, 각도:{angle})")
        return result

    except Exception as e:
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))


def _build_vision_prompt(blob_url, good_refs, bad_refs):
    """22년 경력 원장님 평가 기준 프롬프트"""
    
    system_content = """너는 경력 22년 바버샵 원장님이야.
인스타그램 홍보용 사진의 퀄리티를 평가해줘.

[평가 배경]
원장님은 시술로 바쁘고 홍보 편집 시간이 없어.
고객이 바버샵에 가장 원하는 것: 페이드컷 (1순위)

[평가 기준 - 총 30점]

1. fade_gradient_clarity (8점) - 최우선!
   페이드 그라데이션 선명도
   - 한국인 두상은 울퉁불퉁함 → 굴곡 적고 정교한 샷인가?
   - 뒷면/측면 각도면 가점 (그라데이션 잘 보임)
   - 두피와 머리카락이 자연스럽게 연결되는가?
   
2. styling_appeal (6점)
   앞모습 스타일링 매력
   - 앞모습 각도면 가점
   - 어떤 머리를 해도 앞모습 스타일링 이쁘면 우선
   
3. model_vibe (6점)
   손님 포즈/리액션/분위기
   - 가게 분위기에 어울리는가?
   - 손님이 멋스러운가?
   
4. lighting (5점)
   조명 품질
   
5. background (5점)
   배경 깔끔함

[각도 감지]
사진 각도를 분석해서 detected_angle 필드에 표시:
- "back_side": 뒷면 또는 측면 (페이드 그라데이션 중심)
- "front": 정면 (스타일링 중심)
- "unknown": 판단 불가

[응답 형식] 반드시 JSON으로:
{
  "scores": {
    "fade_gradient_clarity": 0~8,
    "styling_appeal": 0~6,
    "model_vibe": 0~6,
    "lighting": 0~5,
    "background": 0~5
  },
  "total": 0~30,
  "detected_angle": "back_side" | "front" | "unknown",
  "style_tags": ["fade_cut", "side_part" 등],
  "reason": "평가 이유 1줄"
}"""

    messages = [{"role": "system", "content": system_content}]
    
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": blob_url, "detail": "high"}},
            {"type": "text", "text": "이 사진을 원장님 기준으로 평가하고 JSON으로만 응답해줘."}
        ]
    })
    return messages


def _classify_angle(detected: str) -> str:
    """각도 분류 정규화"""
    angle_map = {
        "back_side": "back_side",
        "back": "back_side",
        "side": "back_side",
        "back-side": "back_side",
        "front": "front",
        "unknown": "unknown"
    }
    return angle_map.get(detected.lower(), "unknown")


async def _save_pass_result(shop_id, photo, result):
    """DB 저장"""
    try:
        from services.cosmos_db import save_photo_meta
        now_kst = datetime.now(timezone(timedelta(hours=9))).isoformat()
        doc = {
            "id": photo["image_id"],
            "shop_id": shop_id,
            "blob_url": photo["blob_url"],
            "stage2_pass": True,
            "stage2_tags": result["stage2_tags"],
            "total_score": result["total_score"],
            "fade_cut_score": result["fade_cut_score"],
            "detected_angle": result["detected_angle"],
            "is_usable": True,
            "analyzed_at": now_kst
        }
        save_photo_meta(shop_id, doc)
        print(f"[photo_filter] DB 저장 성공 → {photo['image_id']}")
    except Exception as e:
        print(f"[photo_filter] DB 저장 오류: {e}")


async def _handle_fail_result(shop_id, photo, result):
    pass


def _judge_brightness(score): 
    return "good" if score >= 4 else "dark" if score >= 2 else "bright"


def _make_fail_result(image_id, reason):
    return {
        "image_id": image_id, 
        "stage2_pass": False, 
        "reason": reason, 
        "total_score": 0,
        "detected_angle": "unknown"
    }


async def _load_reference_photos(shop_id: str) -> list:
    """레퍼런스 사진 로드"""
    try:
        from services.cosmos_db import get_album, get_photo_by_id
        
        album_id = f"reference_{shop_id}"
        album = get_album(shop_id, album_id)
        
        if not album:
            print(f"[photo_filter] 레퍼런스 앨범 없음: {album_id}")
            return []
        
        # ✅ 수정: photo_list → photo_ids (지연님 save_album 구조)
        photo_ids = album.get("photo_ids", [])
        
        references = []
        for item in photo_ids:
            # 문자열이면 그대로, 딕셔너리면 photo_id 추출
            if isinstance(item, str):
                photo_id = item
            elif isinstance(item, dict):
                photo_id = item.get("photo_id") or item.get("id")
            else:
                continue
            
            if photo_id:
                photo = get_photo_by_id(shop_id, photo_id)
                if photo:
                    photo["label"] = "good"
                    references.append(photo)
        
        print(f"[photo_filter] 레퍼런스 {len(references)}장 로드 완료")
        return references
        
    except Exception as e:
        print(f"[photo_filter] 레퍼런스 로드 실패: {e}")
        return []