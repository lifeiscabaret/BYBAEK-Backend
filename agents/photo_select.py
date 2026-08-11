"""
[변경 이력 - preferred_styles / exclude_conditions 반영]
- _apply_style_preferences() 추가: 온보딩에서 수집한 preferred_styles/exclude_conditions을
  실제 사진 선택 단계에 반영 (기존엔 brand_settings에 있어도 전혀 사용되지 않았음)
  * exclude_conditions: 매칭되는 사진은 후보에서 하드 제외 (전량 제외되면 안전하게 필터 완화)
  * preferred_styles: 매칭되는 사진에 우선순위 가중치 부여 (제외는 아니고 정렬 우선순위만 상승)
- _categorize_by_angle 정렬 기준에 _preference_boost 반영 (동일 조건이면 선호 스타일 우선)
"""

import json
import anthropic
from datetime import datetime, timezone, timedelta

from utils.claude_auth import CLAUDE_BASE_URL, get_claude_model, get_claude_token

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

    # [NEW] STEP 0: 온보딩 스타일 선호도 반영 (exclude 제외 + preferred 가중치)
    photo_candidates = _apply_style_preferences(photo_candidates, brand_settings)

    # STEP 1: 14일 중복 방지 + 각도별 분류
    categorized = _categorize_by_angle(photo_candidates, max_count=max_photos)

    print(f"[photo_select] 각도별 분류 완료:")
    print(f"  - 뒷면/측면 (페이드): {len(categorized['back_side'])}장")
    print(f"  - 앞면 (스타일링): {len(categorized['front'])}장")
    print(f"  - 분위기: {len(categorized['vibe'])}장")

    # STEP 2: 원장님 조합 패턴 적용
    selected = await _apply_director_pattern(
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


# ──────────────────────────────────────────
# [NEW] 온보딩 스타일 선호도 반영
# ──────────────────────────────────────────

def _apply_style_preferences(candidates: list, brand_settings: dict) -> list:
    """
    preferred_styles / exclude_conditions을 사진 후보 리스트에 반영.

    - exclude_conditions: 사진의 style_tags(=stage2_tags)에 제외 조건 문자열이
      포함되면 후보에서 제거. (예: "얼굴 클로즈업" 지정 시 해당 태그 사진 제외)
    - preferred_styles: 매칭되는 사진에 _preference_boost=1 부여.
      완전 제외가 아니라 정렬 우선순위만 올림 (선호 스타일이 없다고 후보가 0장이 되면 안 되므로).
    - exclude_conditions으로 후보가 전량 걸러지면(사진 태그 품질 이슈 등) 안전하게
      필터를 완화하고 원본 후보를 그대로 반환 — 라이브 파이프라인이 빈 결과로 멈추는 것 방지.
    """
    preferred_styles = brand_settings.get("preferred_styles", [])
    exclude_conditions = brand_settings.get("exclude_conditions", [])

    if not preferred_styles and not exclude_conditions:
        return candidates

    filtered = []
    for photo in candidates:
        tags = photo.get("style_tags", photo.get("stage2_tags", []))
        tags_str = " ".join(tags) if isinstance(tags, list) else str(tags or "")

        if exclude_conditions and any(cond and cond in tags_str for cond in exclude_conditions):
            continue

        photo["_preference_boost"] = 1 if (
            preferred_styles and any(style and style in tags_str for style in preferred_styles)
        ) else 0
        filtered.append(photo)

    if not filtered:
        print(
            f"[photo_select] exclude_conditions({exclude_conditions})으로 후보 전량 제외됨 "
            f"→ 필터 완화, 원본 {len(candidates)}장 그대로 사용"
        )
        for photo in candidates:
            photo.setdefault("_preference_boost", 0)
        return candidates

    print(
        f"[photo_select] 스타일 선호도 적용 → 후보 {len(candidates)}장 → {len(filtered)}장 "
        f"(exclude_conditions={exclude_conditions}, preferred_styles={preferred_styles})"
    )
    return filtered


def _categorize_by_angle(candidates: list, max_count: int = 5) -> dict:
    now_kst = datetime.now(timezone(timedelta(hours=9)))

    back_side = []
    front = []
    vibe = []

    for photo in candidates:
        used_at = photo.get("used_at")
        if used_at:
            used_at_str = used_at.replace("Z", "+00:00")
            used_dt = datetime.fromisoformat(used_at_str)
            if used_dt.tzinfo is None:
                used_dt = used_dt.replace(tzinfo=timezone.utc)
            days_ago = (now_kst - used_dt).days
            if days_ago < REUSE_COOLDOWN_DAYS:
                continue

        angle = photo.get("detected_angle", "unknown")
        scores = photo.get("scores", {})

        if angle == "back_side":
            photo["_sort_score"] = scores.get("fade_gradient_clarity", 0)
            back_side.append(photo)
        elif angle == "front":
            photo["_sort_score"] = scores.get("styling_appeal", 0)
            front.append(photo)

        if scores.get("model_vibe", 0) >= 4:
            photo["_vibe_score"] = scores.get("model_vibe", 0)
            vibe.append(photo)

    # [NEW] 정렬 기준에 _preference_boost 우선 반영 (동점이면 preferred_styles 매칭 사진 우선)
    back_side.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_sort_score", 0)), reverse=True)
    front.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_sort_score", 0)), reverse=True)
    vibe.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_vibe_score", 0)), reverse=True)

    categorized_ids = {p["id"] for p in back_side + front + vibe}
    if len(categorized_ids) < max_count:
        needed = max_count - len(categorized_ids)
        print(f"[photo_select] 쿨다운 완화 → 부족분 {needed}장을 가장 오래된 사진으로 보충 "
              f"(쿨다운 미적용 {len(categorized_ids)}장, 목표 {max_count}장)")
        sorted_by_used = sorted(
            candidates,
            key=lambda x: x.get("used_at") or "2000-01-01T00:00:00"
        )
        supplement = [p for p in sorted_by_used if p["id"] not in categorized_ids][:needed]
        for photo in supplement:
            angle = photo.get("detected_angle", "unknown")
            scores = photo.get("scores", {})
            if angle == "back_side":
                photo["_sort_score"] = scores.get("fade_gradient_clarity", 0)
                back_side.append(photo)
            elif angle == "front":
                photo["_sort_score"] = scores.get("styling_appeal", 0)
                front.append(photo)
            else:
                photo["_vibe_score"] = scores.get("model_vibe", 0)
                vibe.append(photo)

        back_side.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_sort_score", 0)), reverse=True)
        front.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_sort_score", 0)), reverse=True)
        vibe.sort(key=lambda x: (x.get("_preference_boost", 0), x.get("_vibe_score", 0)), reverse=True)

    return {"back_side": back_side, "front": front, "vibe": vibe}


async def _apply_director_pattern(
    categorized: dict,
    trend_data: dict,
    brand_settings: dict,
    min_count: int,
    max_count: int
) -> list:
    fade_2 = categorized["back_side"][:2]
    style_1 = categorized["front"][:1]
    vibe_1 = categorized["vibe"][:1]

    base_selection = fade_2 + style_1 + vibe_1

    seen_ids = set()
    deduped = []
    for p in base_selection:
        if p["id"] not in seen_ids:
            seen_ids.add(p["id"])
            deduped.append(p)
    base_selection = deduped
    selected_ids = seen_ids

    if len(base_selection) < min_count:
        remaining = [p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
                    if p["id"] not in selected_ids]
        base_selection += remaining[:min_count - len(base_selection)]

    if len(base_selection) > max_count:
        base_selection = base_selection[:max_count]

    if max_count >= 5 and len(base_selection) < max_count:
        expanded = await _gpt_expand_selection(
            base_selection, categorized, trend_data, brand_settings, max_count
        )
        return expanded

    print(f"[photo_select] 원장님 패턴 → 페이드 {len(fade_2)}장 + 스타일링 {len(style_1)}장 + 분위기 {len(vibe_1)}장")
    return base_selection


async def _gpt_expand_selection(
    base_selection, categorized, trend_data, brand_settings, max_count
) -> list:
    already_selected = {p["id"] for p in base_selection}

    additional_candidates = [
        p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
        if p["id"] not in already_selected
    ][:10]

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

    try:
        client = anthropic.AsyncAnthropic(
            base_url=CLAUDE_BASE_URL,
            auth_token=get_claude_token()
        )
        response = await client.messages.create(
            model=get_claude_model(),
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        add_ids = result.get("add_photo_ids", [])
        reason = result.get("reason", "")
        print(f"[photo_select] Claude 확장 → {len(add_ids)}장 추가 | 이유: {reason}")

        id_to_photo = {p["id"]: p for p in additional_candidates}
        additional = [id_to_photo[pid] for pid in add_ids if pid in id_to_photo]

        final = base_selection + additional

        if len(final) > max_count:
            final = final[:max_count]

        if len(final) < max_count:
            all_remaining = [
                p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
                if p["id"] not in {x["id"] for x in final}
            ]
            fill = all_remaining[:max_count - len(final)]
            final += fill
            print(f"[photo_select] 자동 보충 → {len(fill)}장 추가 (최종 {len(final)}장)")

        return final

    except Exception as e:
        print(f"[photo_select] Claude 확장 실패 ({e}) → 기본 조합만 사용")
        all_remaining = [
            p for p in categorized["back_side"] + categorized["front"] + categorized["vibe"]
            if p["id"] not in {x["id"] for x in base_selection}
        ]
        fill = all_remaining[:max_count - len(base_selection)]
        return base_selection + fill


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