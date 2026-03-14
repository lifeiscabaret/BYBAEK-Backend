import os
import json
from datetime import datetime, timezone, timedelta

from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory

REUSE_COOLDOWN_DAYS = 14


async def photo_select_agent(
    shop_id: str,
    trend_data: dict,
    photo_candidates: list,
    brand_settings: dict
) -> list:
    """
    사진 선택 메인 함수
    
    원장님 조합 전략:
    1. 페이드 2장 (뒷면/측면)
    2. 스타일링 1장 (앞모습)
    3. 분위기 1장
    """
    print(f"[photo_select] 시작 → shop_id={shop_id}, 후보={len(photo_candidates)}장")

    min_photos = brand_settings.get("photo_range", {}).get("min", 1)
    max_photos = brand_settings.get("photo_range", {}).get("max", 5)

    if not photo_candidates:
        print(f"[photo_select] 후보 없음 → 빈 리스트 반환")
        return []

    # STEP 1: 14일 중복 방지 + 각도별 분류
    categorized = _categorize_by_angle(photo_candidates)
    
    print(f"[photo_select] 각도별 분류 완료:")
    print(f"  - 뒷면/측면 (페이드): {len(categorized['back_side'])}장")
    print(f"  - 앞면 (스타일링): {len(categorized['front'])}장")
    print(f"  - 분위기: {len(categorized['vibe'])}장")

    # STEP 2: 원장님 조합 패턴 적용
    kernel = _init_kernel()
    selected = await _apply_director_pattern(
        kernel=kernel,
        categorized=categorized,
        trend_data=trend_data,
        brand_settings=brand_settings,
        min_count=min_photos,
        max_count=max_photos
    )

    # 선택된 사진 used_at 업데이트 (14일 재사용 방지)
    await _update_used_at(shop_id, selected)

    print(f"[photo_select] 완료 → {len(selected)}장 선택")
    return selected


def _categorize_by_angle(candidates: list) -> dict:
    """
    사진을 각도별로 분류
    
    Returns:
        {
            "back_side": [...],  # 뒷면/측면 (페이드 그라데이션)
            "front": [...],      # 앞면 (스타일링)
            "vibe": [...]        # 분위기 (model_vibe 점수 높은 것)
        }
    """
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    
    back_side = []
    front = []
    vibe = []
    
    for photo in candidates:
        # 14일 중복 방지
        used_at = photo.get("used_at")
        if used_at:
            used_at_str = used_at.replace("Z", "+00:00")
            used_dt = datetime.fromisoformat(used_at_str)
            if used_dt.tzinfo is None:
                used_dt = used_dt.replace(tzinfo=timezone.utc)
            days_ago = (now_kst - used_dt).days
            if days_ago < REUSE_COOLDOWN_DAYS:
                continue
        
        # 각도 분류
        angle = photo.get("detected_angle", "unknown")
        scores = photo.get("scores", {})
        
        if angle == "back_side":
            # 페이드 그라데이션 점수 순 정렬
            photo["_sort_score"] = scores.get("fade_gradient_clarity", 0)
            back_side.append(photo)
            
        elif angle == "front":
            # 스타일링 점수 순 정렬
            photo["_sort_score"] = scores.get("styling_appeal", 0)
            front.append(photo)
        
        # model_vibe 높은 것은 분위기용으로
        if scores.get("model_vibe", 0) >= 4:
            photo["_vibe_score"] = scores.get("model_vibe", 0)
            vibe.append(photo)
    
    # 정렬
    back_side.sort(key=lambda x: x.get("_sort_score", 0), reverse=True)
    front.sort(key=lambda x: x.get("_sort_score", 0), reverse=True)
    vibe.sort(key=lambda x: x.get("_vibe_score", 0), reverse=True)
    
    return {
        "back_side": back_side,
        "front": front,
        "vibe": vibe
    }


async def _apply_director_pattern(
    kernel: Kernel,
    categorized: dict,
    trend_data: dict,
    brand_settings: dict,
    min_count: int,
    max_count: int
) -> list:
    """
    원장님 조합 패턴 적용
    
    기본 조합 (4장):
    - 페이드 2장 (뒷면/측면)
    - 스타일링 1장 (앞모습)
    - 분위기 1장
    
    확장 (5~20장):
    - 위 비율 유지하면서 확장
    """
    
    # 기본 조합 구성
    fade_2 = categorized["back_side"][:2]
    style_1 = categorized["front"][:1]
    vibe_1 = categorized["vibe"][:1]
    
    base_selection = fade_2 + style_1 + vibe_1
    
    # 중복 제거 (vibe에 이미 포함된 사진 제외)
    selected_ids = {p["id"] for p in base_selection}
    base_selection = [p for i, p in enumerate(base_selection) if p["id"] not in list(selected_ids)[:i]]
    
    # min 미달 시 보충
    if len(base_selection) < min_count:
        remaining = [p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
                    if p["id"] not in selected_ids]
        base_selection += remaining[:min_count - len(base_selection)]
    
    # max 초과 시 자르기
    if len(base_selection) > max_count:
        base_selection = base_selection[:max_count]
    
    # max가 5장 이상이면 GPT에게 확장 전략 물어보기
    if max_count >= 5 and len(base_selection) < max_count:
        expanded = await _gpt_expand_selection(
            kernel, base_selection, categorized, trend_data, brand_settings, max_count
        )
        return expanded
    
    print(f"[photo_select] 원장님 패턴 → 페이드 {len(fade_2)}장 + 스타일링 {len(style_1)}장 + 분위기 {len(vibe_1)}장")
    return base_selection


async def _gpt_expand_selection(
    kernel, base_selection, categorized, trend_data, brand_settings, max_count
) -> list:
    """
    GPT가 기본 조합을 확장 (5~20장)
    
    원장님 비율 유지:
    - 페이드 40%
    - 스타일링 20%
    - 분위기 20%
    - 트렌드 매칭 20%
    """
    
    already_selected = {p["id"] for p in base_selection}
    
    # 추가 후보
    additional_candidates = [
        p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
        if p["id"] not in already_selected
    ][:10]  # GPT에 최대 10장만 전달
    rag_reference = brand_settings.get("rag_reference", "")
    reference_line = f"\n[레퍼런스 샵 스타일]\n{rag_reference}\n이 샵의 피드 톤/구도를 참고해서 선택해줘." if rag_reference else ""

    prompt = f"""너는 바버샵 사진 큐레이터야.
기본 조합 {len(base_selection)}장을 최대 {max_count}장으로 확장해줘.

[원장님 조합 원칙]
- 페이드 그라데이션 (뒷면/측면): 40%
- 스타일링 (앞모습): 20%
- 분위기 (손님 포즈): 20%
- 트렌드 매칭: 20%
{reference_line}
[오늘 트렌드]
{trend_data.get("trend", "정보 없음")}

[현재 선택된 사진]
{json.dumps([{"id": p["id"], "angle": p.get("detected_angle"), "tags": p.get("stage2_tags", [])} for p in base_selection], ensure_ascii=False)}

[추가 후보]
{json.dumps([{"id": p["id"], "angle": p.get("detected_angle"), "fade_score": p.get("fade_cut_score"), "tags": p.get("stage2_tags", [])} for p in additional_candidates], ensure_ascii=False)}

몇 장을 더 추가할지, 어떤 사진을 추가할지 결정해줘.
페이드 그라데이션 비중이 가장 높아야 해.

응답 형식 (JSON만):
{{
  "add_photo_ids": ["photo_id_1", "photo_id_2"],
  "reason": "확장 이유 1줄"
}}
"""

    chat_history = ChatHistory()
    chat_history.add_user_message(prompt)

    try:
        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        raw = str(response).strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        add_ids = result.get("add_photo_ids", [])
        reason = result.get("reason", "")
        print(f"[photo_select] GPT 확장 → {len(add_ids)}장 추가 | 이유: {reason}")

        # 실제 사진 추가
        id_to_photo = {p["id"]: p for p in additional_candidates}
        additional = [id_to_photo[pid] for pid in add_ids if pid in id_to_photo]
        
        final = base_selection + additional
        
        # max 초과 시 자르기
        if len(final) > max_count:
            final = final[:max_count]
        
        return final

    except Exception as e:
        print(f"[photo_select] GPT 확장 실패 ({e}) → 기본 조합만 사용")
        return base_selection


async def _update_used_at(shop_id: str, selected: list):
    from services.cosmos_db import save_photo_meta
    now_kst = datetime.now(timezone(timedelta(hours=9))).isoformat()
    for photo in selected:
        try:
            doc = {
                "id":      photo.get("id") or photo.get("image_id"),
                "used_at": now_kst
            }
            save_photo_meta(shop_id, doc)
        except Exception as e:
            print(f"[photo_select] used_at 업데이트 실패 ({photo.get('id')}): {e}")


def _init_kernel() -> Kernel:
    deployment = os.getenv(
        "AZURE_OPENAI_DEPLOYMENT_MINI",
        os.getenv("AZURE_OPENAI_DEPLOYMENT")
    )
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel