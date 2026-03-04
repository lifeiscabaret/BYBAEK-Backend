import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from openai import AsyncAzureOpenAI

# [설정값]

# 2차 필터링 PASS 임계값 (총점 25점 기준)
STAGE2_PASS_THRESHOLD = 15      # 15점 이상 PASS (60%)

# 병렬 처리 동시 요청 수 (Azure OpenAI rate limit 고려)
MAX_CONCURRENT = 5

# 프롬프트에 넣을 레퍼런스 사진 수
MAX_GOOD_EXAMPLES = 5           
MAX_BAD_EXAMPLES  = 3         

# [메인 함수] 1차 필터링에서 호출
async def run_stage2_filter(
    shop_id: str,
    stage1_pass_list: list
) -> dict:
    """
    2차 필터링 메인 함수
    Args:
        shop_id:          샵 ID
        stage1_pass_list: 1차 통과 사진 리스트

    Returns:
        {
          "total":   100,
          "passed":  72,
          "failed":  28,
          "results": [{"image_id": "...", "pass": true, "score": 0.88}, ...]
        }
    """
    print(f"[photo_filter] 2차 필터링 시작 → shop_id={shop_id}, "
          f"대상={len(stage1_pass_list)}장")

    if not stage1_pass_list:
        print("[photo_filter] 대상 사진 없음 → 종료")
        return {"total": 0, "passed": 0, "failed": 0, "results": []}

    # STEP 1: CosmosDB에서 레퍼런스 사진 로드
    # TODO: from services.cosmos_db import get_reference_photos
    # reference_photos = await get_reference_photos(shop_id)
    reference_photos = _mock_reference_photos()  # 목업: 라벨링 전까지 사용

    good_refs = [p for p in reference_photos if p.get("label") == "good"][:MAX_GOOD_EXAMPLES]
    bad_refs  = [p for p in reference_photos if p.get("label") == "bad"][:MAX_BAD_EXAMPLES]

    print(f"[photo_filter] 레퍼런스 로드 → "
          f"좋은 예시 {len(good_refs)}장, 나쁜 예시 {len(bad_refs)}장")

    # STEP 2: 병렬 처리 (MAX_CONCURRENT 제한)
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

    # STEP 3: 결과 처리 (PASS → 저장, FAIL → 삭제)
    passed, failed = [], []
    for photo, result in zip(stage1_pass_list, results):
        if isinstance(result, Exception):
            print(f"[photo_filter] 평가 오류 ({photo['image_id']}): {result}")
            result = _make_fail_result(photo["image_id"], "evaluation_error")

        if result.get("stage2_pass"):
            passed.append(result)
            # PASS: CosmosDB에 메타데이터 저장
            await _save_pass_result(shop_id, photo, result)
        else:
            failed.append(result)
            # FAIL: Blob 삭제 + CosmosDB 상태 업데이트
            await _handle_fail_result(shop_id, photo, result)

    print(f"[photo_filter] 완료 → PASS {len(passed)}장 / FAIL {len(failed)}장")
    return {
        "total":   len(stage1_pass_list),
        "passed":  len(passed),
        "failed":  len(failed),
        "results": passed + failed
    }

# [핵심 평가] GPT-4o Vision으로 사진 평가
async def _evaluate_photo(
    image_id: str,
    blob_url: str,
    stage1_data: dict,
    good_refs: list,
    bad_refs: list
) -> dict:
    """
    GPT-4o Vision으로 사진 1장 평가

    평가 구조:
      1단계: 평가 기준 명문화 (5개 항목)
      2단계: 항목별 0~5점 점수화 (총점 25점)
      3단계: Few-shot 예시 (레퍼런스 있으면 자동 삽입)

    Returns:
        {
          "image_id":      "img_001",
          "stage2_pass":   true,
          "stage2_score":  0.88,
          "stage2_tags":   ["fade_cut", "side_part"],
          "promo_ok_prob": 0.88,
          "scores":        {"gradient": 5, "lighting": 4, ...},
          "total_score":   22,
          "reason":        "페이드 그라데이션 자연스럽고 조명 좋음",
          "fade_cut_score": 0.92,
          "brightness":    "good",
          "sharpness":     "high"
        }
    """
    print(f"[photo_filter] 평가 중 → {image_id}")

    # 프롬프트 메시지 구성
    messages = _build_vision_prompt(blob_url, good_refs, bad_refs)

    client = AsyncAzureOpenAI(
        api_key=os.getenv("AZURE_OPENAI_KEY"),
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    )

    deployment = os.getenv(
        "AZURE_OPENAI_DEPLOYMENT_FULL",
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )

    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=messages,
            max_tokens=500
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        gpt_result = json.loads(raw)

        # 점수 정규화 (0~1 사이로 변환)
        total_score   = gpt_result.get("total", 0)
        promo_ok_prob = round(total_score / 25, 2)
        stage2_pass   = total_score >= STAGE2_PASS_THRESHOLD

        # fade_cut_score: gradient 점수를 0~1로 변환
        scores        = gpt_result.get("scores", {})
        fade_cut_score = round(scores.get("gradient", 0) / 5, 2)

        # 밝기/선명도 판정
        brightness = _judge_brightness(scores.get("lighting", 3))
        sharpness  = _judge_sharpness(scores.get("sharpness", 3))

        result = {
            "image_id":       image_id,
            "stage2_pass":    stage2_pass,
            "stage2_score":   promo_ok_prob,
            "stage2_tags":    gpt_result.get("style_tags", []),
            "promo_ok_prob":  promo_ok_prob,
            "scores":         scores,
            "total_score":    total_score,
            "reason":         gpt_result.get("reason", ""),
            "fade_cut_score": fade_cut_score,
            "brightness":     brightness,
            "sharpness":      sharpness
        }

        status = "✅ PASS" if stage2_pass else "❌ FAIL"
        print(f"[photo_filter] {status} {image_id} → "
              f"{total_score}/25점 | {gpt_result.get('reason', '')[:50]}")
        return result

    except Exception as e:
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))


def _build_vision_prompt(
    blob_url: str,
    good_refs: list,
    bad_refs: list
) -> list:
    """
    GPT-4o Vision 프롬프트 구성

    구조:
      1단계: 역할 + 평가 기준 명문화
      2단계: 점수화 기준
      3단계: Few-shot 예시 (레퍼런스 있으면 자동 삽입)
      4단계: 평가 대상 사진
    """
    # 시스템 프롬프트: 역할 + 평가 기준
    system_content = """너는 경력 20년의 바버샵 전문가이자 인스타그램 마케터야.
바버샵 홍보용 사진의 퀄리티를 전문가 기준으로 평가해줘.

[평가 기준]
1. gradient (페이드 그라데이션): 그라데이션이 자연스럽고 경계가 뭉치지 않을 것 (0~5점)
2. lighting (조명/노출): 너무 어둡거나 과노출되지 않고 자연스러울 것 (0~5점)
3. background (배경): 복잡하거나 지저분하지 않고 깔끔할 것 (0~5점)
4. model_vibe (모델/분위기): 모델 표정이 자연스럽고 홍보용으로 적합할 것 (0~5점)
5. sharpness (선명도/구도): 핀트가 맞고 구도가 홍보용으로 적합할 것 (0~5점)

[통과 기준]
- 총점 25점 기준 15점 이상 PASS
- 어느 한 항목이라도 1점 이하면 즉시 FAIL (치명적 결함)

[style_tags 추출 규칙]
사진에서 보이는 스타일을 아래 목록에서 선택:
fade_cut, side_part, slick_back, two_block, ivy_league,
mullet, buzz_cut, pompadour, french_crop, textured

[응답 형식] 반드시 JSON으로만:
{
  "scores": {
    "gradient": 0~5,
    "lighting": 0~5,
    "background": 0~5,
    "model_vibe": 0~5,
    "sharpness": 0~5
  },
  "total": 0~25,
  "pass": true/false,
  "style_tags": ["태그1", "태그2"],
  "reason": "평가 이유 한 줄"
}"""

    messages = [{"role": "system", "content": system_content}]

    # Few-shot 예시 구성 (레퍼런스 있을 때만)
    if good_refs or bad_refs:
        few_shot_content = []

        if good_refs:
            few_shot_content.append({
                "type": "text",
                "text": "✅ 아래는 원장님이 직접 선택한 [좋은 예시] 사진들이야. 이 기준을 참고해:"
            })
            for ref in good_refs:
                few_shot_content.append({
                    "type": "image_url",
                    "image_url": {"url": ref["blob_url"], "detail": "low"}
                })
                few_shot_content.append({
                    "type": "text",
                    "text": f"→ 좋은 이유: {ref.get('reason', '원장님 선택 기준')}"
                })

        if bad_refs:
            few_shot_content.append({
                "type": "text",
                "text": "❌ 아래는 원장님이 탈락시킨 [나쁜 예시] 사진들이야:"
            })
            for ref in bad_refs:
                few_shot_content.append({
                    "type": "image_url",
                    "image_url": {"url": ref["blob_url"], "detail": "low"}
                })
                few_shot_content.append({
                    "type": "text",
                    "text": f"→ 탈락 이유: {ref.get('reason', '원장님 탈락 기준')}"
                })

        messages.append({"role": "user", "content": few_shot_content})
        messages.append({
            "role": "assistant",
            "content": "네, 원장님 기준을 이해했어요. 평가할 사진을 보여주세요."
        })

    # 평가 대상 사진
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": blob_url, "detail": "high"}  # 평가 대상은 high
            },
            {
                "type": "text",
                "text": "이 사진을 평가 기준에 따라 채점하고 JSON으로 응답해줘."
            }
        ]
    })

    return messages


# ──────────────────────────────────────────
# [결과 처리]
# ──────────────────────────────────────────

async def _save_pass_result(shop_id: str, photo: dict, result: dict):
    """
    PASS 사진 CosmosDB에 메타데이터 저장

    photo_select.py가 읽을 photo_meta 문서 형태로 저장.
    """
    now_kst = datetime.now(timezone(timedelta(hours=9))).isoformat()

    doc = {
        "id":            photo["image_id"],
        "photo_id":      photo["image_id"],
        "shop_id":       shop_id,
        "blob_url":      photo["blob_url"],

        # 1차 필터링 결과 (태경님)
        "stage1_score":  photo.get("stage1_score", 0),
        "stage1_reason": photo.get("stage1_reason", ""),

        # 2차 필터링 결과 (지현)
        "stage2_score":  result["stage2_score"],
        "stage2_pass":   True,
        "stage2_tags":   result["stage2_tags"],
        "promo_ok_prob": result["promo_ok_prob"],

        # photo_select.py에서 사용하는 필드
        "style_tags":    result["stage2_tags"],
        "fade_cut_score": result["fade_cut_score"],
        "brightness":    result["brightness"],
        "sharpness":     result["sharpness"],
        "is_usable":     True,
        "analyzed_at":   now_kst,
        "used_at":       None,   # 아직 사용 안 함

        # 촬영 메타데이터 (태경님이 넘겨준 것)
        "taken_at":      photo.get("metadata", {}).get("taken_at"),
        "resolution":    photo.get("metadata", {}).get("resolution")
    }

    # TODO: from services.cosmos_db import save_photo_meta
    # await save_photo_meta(doc)
    print(f"[photo_filter] CosmosDB 저장 → {photo['image_id']} (목업)")


async def _handle_fail_result(shop_id: str, photo: dict, result: dict):
    """
    FAIL 사진 처리: Blob 삭제 + CosmosDB 상태 업데이트
    """
    # TODO: from services.blob_storage import delete_blob
    # await delete_blob(photo["blob_url"])
    print(f"[photo_filter] Blob 삭제 → {photo['image_id']} (목업)")

    # TODO: from services.cosmos_db import update_photo_stage2
    # await update_photo_stage2(photo["image_id"], {
    #     "stage2_pass": False,
    #     "stage2_score": result["stage2_score"],
    #     "stage2_reason": result["reason"],
    #     "is_usable": False
    # })

# [유틸]
def _judge_brightness(lighting_score: int) -> str:
    """lighting 점수 → brightness 문자열 변환"""
    if lighting_score >= 4:
        return "good"
    elif lighting_score >= 2:
        return "dark"
    else:
        return "bright"  # 과노출


def _judge_sharpness(sharpness_score: int) -> str:
    """sharpness 점수 → sharpness 문자열 변환"""
    if sharpness_score >= 4:
        return "high"
    elif sharpness_score >= 2:
        return "medium"
    else:
        return "low"


def _make_fail_result(image_id: str, reason: str) -> dict:
    """평가 실패 또는 FAIL 시 기본 결과 반환"""
    return {
        "image_id":       image_id,
        "stage2_pass":    False,
        "stage2_score":   0.0,
        "stage2_tags":    [],
        "promo_ok_prob":  0.0,
        "scores":         {},
        "total_score":    0,
        "reason":         reason,
        "fade_cut_score": 0.0,
        "brightness":     "dark",
        "sharpness":      "low"
    }


def _mock_reference_photos() -> list:
    """
    목업 레퍼런스 사진 (라벨링 세션 전까지 사용)
    라벨링 완료 후 CosmosDB get_reference_photos()로 교체 예정.
    # TODO: from services.cosmos_db import get_reference_photos
    """
    return []   # 빈 리스트 → Few-shot 없이 기준만으로 동작

# [목업 테스트] 단독 실행용
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv() 

    # 태경님 1차 통과 리스트 목업
    mock_stage1_pass_list = [
        {
            "image_id":      "img_001",
            "blob_url":      "https://blob.../photo_001.jpg",
            "stage1_score":  0.88,
            "stage1_reason": "pass",
            "metadata":      {"taken_at": "2026-02-01", "resolution": "4032x3024"}
        },
        {
            "image_id":      "img_002",
            "blob_url":      "https://blob.../photo_002.jpg",
            "stage1_score":  0.75,
            "stage1_reason": "pass",
            "metadata":      {"taken_at": "2026-02-05", "resolution": "3024x4032"}
        }
    ]

    async def test():
        print("=" * 50)
        print("[테스트] 2차 필터링 (목업 - Azure 연결 없음)")
        print("=" * 50)
        print("※ GPT-4o Vision은 Azure 연결 필요. 구조 확인만 진행.")

        # 프롬프트 구조 확인 (Azure 없이)
        messages = _build_vision_prompt(
            blob_url="https://blob.../test.jpg",
            good_refs=[],
            bad_refs=[]
        )
        print(f"\n[프롬프트 구조]")
        print(f"  메시지 수: {len(messages)}개")
        print(f"  시스템 프롬프트 길이: {len(messages[0]['content'])}자")
        print(f"  마지막 메시지 타입: {type(messages[-1]['content'])}")
        print("\n✅ 프롬프트 구조 정상")

        # 결과 처리 구조 확인
        mock_result = {
            "image_id":       "img_001",
            "stage2_pass":    True,
            "stage2_score":   0.88,
            "stage2_tags":    ["fade_cut", "side_part"],
            "promo_ok_prob":  0.88,
            "scores":         {"gradient": 5, "lighting": 4, "background": 4,
                               "model_vibe": 4, "sharpness": 5},
            "total_score":    22,
            "reason":         "페이드 그라데이션 자연스럽고 조명 좋음",
            "fade_cut_score": 1.0,
            "brightness":     "good",
            "sharpness":      "high"
        }
        print(f"\n[PASS 결과 예시]")
        print(json.dumps(mock_result, ensure_ascii=False, indent=2))

    import asyncio
    asyncio.run(test())