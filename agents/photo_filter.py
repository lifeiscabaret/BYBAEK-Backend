import os
import json
import asyncio
from datetime import datetime, timezone, timedelta
from openai import AsyncAzureOpenAI

# [설정값]
STAGE2_PASS_THRESHOLD = 15      # 25점 중 15점 이상 PASS
MAX_CONCURRENT = 5              # 병렬 처리 제한
MAX_GOOD_EXAMPLES = 5           
MAX_BAD_EXAMPLES  = 3         

# [메인 함수]
async def run_stage2_filter(
    shop_id: str,
    stage1_pass_list: list
) -> dict:
    print(f"[photo_filter] 2차 필터링 시작 → shop_id={shop_id}, 대상={len(stage1_pass_list)}장")

    if not stage1_pass_list:
        print("[photo_filter] 대상 사진 없음 → 종료")
        return {"total": 0, "passed": 0, "failed": 0, "results": []}

    # STEP 1: 레퍼런스 로드 (실제 DB에서 조회)
    reference_photos = await _load_reference_photos(shop_id)
    good_refs = [p for p in reference_photos if p.get("label") == "good"][:MAX_GOOD_EXAMPLES]
    bad_refs  = [p for p in reference_photos if p.get("label") == "bad"][:MAX_BAD_EXAMPLES]

    print(f"[photo_filter] 레퍼런스 로드 → 좋은 예시 {len(good_refs)}장, 나쁜 예시 {len(bad_refs)}장")
    
    # 레퍼런스 없어도 계속 진행 (fallback)
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
            # PASS 시 CosmosDB 저장 시도 (에러 나도 파이프라인 유지)
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

# [핵심 평가] GPT Vision 분석
async def _evaluate_photo(
    image_id: str,
    blob_url: str,
    stage1_data: dict,
    good_refs: list,
    bad_refs: list
) -> dict:
    print(f"[photo_filter] 평가 중 → {image_id}")

    # 프롬프트 구성
    messages = _build_vision_prompt(blob_url, good_refs, bad_refs)

    # 환경변수 로드 (지현님의 .env에 맞춰 유연하게 설정)
    api_key = os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_KEY")
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    
    # 배포 이름 결정 (지현님이 배포한 'Name'을 우선 조회)
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
        promo_ok_prob = round(total_score / 25, 2)
        stage2_pass   = total_score >= STAGE2_PASS_THRESHOLD

        scores        = gpt_result.get("scores", {})
        fade_cut_score = round(scores.get("gradient", 0) / 5, 2)

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
            "brightness":     _judge_brightness(scores.get("lighting", 3)),
            "sharpness":      _judge_sharpness(scores.get("sharpness", 3))
        }

        status = "✅ PASS" if stage2_pass else "❌ FAIL"
        print(f"[photo_filter] {status} {image_id} → {total_score}/25점")
        return result

    except Exception as e:
        print(f"[photo_filter] GPT 평가 실패 ({image_id}): {e}")
        return _make_fail_result(image_id, str(e))

def _build_vision_prompt(blob_url, good_refs, bad_refs):
    system_content = """너는 경력 20년의 바버샵 전문가이자 인스타그램 마케터야.
바버샵 홍보용 사진의 퀄리티를 전문가 기준으로 평가해줘.

[평가 기준 (0~5점)]
1. gradient: 페이드 그라데이션의 자연스러움
2. lighting: 적절한 조명과 노출
3. background: 배경의 깔끔함
4. model_vibe: 모델의 분위기와 홍보 적합성
5. sharpness: 초점과 구도

[응답 형식] 반드시 JSON으로:
{
  "scores": {"gradient":0, "lighting":0, "background":0, "model_vibe":0, "sharpness":0},
  "total": 0~25,
  "style_tags": ["fade_cut", "side_part" 등],
  "reason": "평가 이유"
}"""
    messages = [{"role": "system", "content": system_content}]
    
    # 퓨샷 예시 생략 (필요 시 추가 가능)
    messages.append({
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": blob_url, "detail": "high"}},
            {"type": "text", "text": "이 사진을 평가하고 JSON으로만 응답해줘."}
        ]
    })
    return messages

async def _save_pass_result(shop_id, photo, result):
    # DB 저장 로직 (현재 지현님 환경에서 NotFound 에러 방지를 위해 try-except 권장)
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
            "is_usable": True,
            "analyzed_at": now_kst
        }
        save_photo_meta(shop_id, doc)
        print(f"[photo_filter] CosmosDB 저장 성공 → {photo['image_id']}")
    except Exception as e:
        print(f"[photo_filter] CosmosDB 저장 오류: {e}")

async def _handle_fail_result(shop_id, photo, result):
    # 탈락 사진 처리 (필요 시 삭제 로직 추가)
    pass

def _judge_brightness(score): return "good" if score >= 4 else "dark" if score >= 2 else "bright"
def _judge_sharpness(score): return "high" if score >= 4 else "medium" if score >= 2 else "low"
def _make_fail_result(image_id, reason):
    return {"image_id": image_id, "stage2_pass": False, "reason": reason, "total_score": 0}

# [테스트용 목업]
async def _load_reference_photos(shop_id: str) -> list:
    """
    온보딩에서 사장님이 선택한 레퍼런스 사진 3장 조회
    
    onboarding.py에서 save_album(album_id=f"reference_{shop_id}")로 저장한 것을 조회
    """
    try:
        from services.cosmos_db import get_album
        
        album_id = f"reference_{shop_id}"
        album = get_album(shop_id, album_id)
        
        if not album:
            print(f"[photo_filter] 레퍼런스 앨범 없음: {album_id}")
            return []
        
        # album 구조: {"photo_list": [{"photo_id": "..."}, ...], ...}
        photo_list = album.get("photo_list", [])
        
        # photo_id만 있으므로 실제 사진 메타 조회
        from services.cosmos_db import get_photo_by_id
        
        references = []
        for item in photo_list:
            photo_id = item.get("photo_id")
            if photo_id:
                photo = get_photo_by_id(shop_id, photo_id)
                if photo:
                    # label은 기본적으로 "good" (온보딩에서 좋은 예시만 선택)
                    photo["label"] = "good"
                    references.append(photo)
        
        print(f"[photo_filter] 레퍼런스 {len(references)}장 로드 완료")
        return references
        
    except Exception as e:
        print(f"[photo_filter] 레퍼런스 로드 실패: {e}")
        return []  # 에러 나도 파이프라인 계속 진행


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print("구조 확인 완료. 실제 실행은 파이프라인을 통해 진행하세요.")