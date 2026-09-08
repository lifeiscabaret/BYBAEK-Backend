# PART 2 초안 — 관련성 게이트를 채점에서 분리 (구조 개선)

> 상태: **초안(미적용).** 순서상 수정 A 배포 + 5회 베이스라인 확정 **후**에 착수.
> 커밋 분리 원칙: 수정 A와 절대 한 커밋에 섞지 않는다. PART 2 단독 커밋이어야
> 동일 5회 프로토콜로 PART 2 효과만 분리 측정 가능.
> 대상: `agents/photo_filter.py` — GPT 응답 스키마 + `_evaluate_photo` 게이트 로직.

---

## 문제(버그 A) 재확인
관련성 판단이 `photo_category` 분류 결과 **하나에만** 의존한다. 분류기가 한 번 틀리면
관련성 게이트가 통째로 무력화되고, 그 뒤엔 25점 채점(=사진이 잘 찍혔는가)만 남는다.
barber_portrait은 gradient·model_vibe 둘 다 instant_fail에서 빠져 lighting/background/
sharpness 3개만 남는데, 이 3개는 바버샵과 무관해도 잘 찍히면 높게 나온다 → 잘 찍힌
무관 인물 사진이 통과(g18~g23 실측 오통과).

## 핵심 방향
"바버샵 관련성"을 **채점표와 독립된 이진 판정**으로 분리한다. 카테고리 분류가 틀려도
관련성 게이트가 독립 작동하는 **이중 안전장치**.

---

## 변경 1 — GPT 응답 스키마에 필드 2개 추가
```json
{
  "barber_technique_visible": true | false,
  "technique_evidence": "좌측 사이드에 스킨페이드 그라데이션이 명확히 보임",
  "photo_category": "...",
  "scores": { ... },
  ...
}
```
- `barber_technique_visible`: 바버샵에서 하는 시술 형태가 실제로 **식별되는가**만 판단.
  성별·출처·매장맥락·포즈·장소·"헤어가 주제인지" 전부 무관 (기준 문서 그대로).
- `technique_evidence`: true일 때 **어떤 시술이 어디에 보이는지 구체적으로 지목 강제**.
  - 짧은 스타일 예: "좌측 사이드 스킨페이드", "후두부 균일 버즈 라인".
  - 긴 스타일 예(형태 근거): "윗머리 짧고 뒷머리 긴 멀릿 실루엣", "뒤로 넘긴 슬릭백 형태",
    "정돈된 롱트림 라인". → **긴 스타일은 형태(실루엣/길이배분/넘김/라인)로 지목**.
  - false면 빈 문자열.
  → "잘 찍혔으니 대충 true" 및 "짧으니까 바버컷이겠지"류 판정을 막는 장치.
  → ⚠️ 5-3: 근거로 "다듬은 흔적/신선도"를 요구하지 말 것. 긴 스타일은 신선도 판별이
     불가하므로 형태 근거만으로 충분. 신선도를 요구하면 긴 바버 시술이 전부 false가 됨.
  (지현 결정 3+4가 겹쳐 생기는 "짧은 머리 아무 사진" 위험을 근거 강제로 차단.)

## 변경 2 — 게이트 로직 (`_evaluate_photo`, photo_filter.py ~331)
```python
# 관련성 이진 게이트 — 채점과 독립. 카테고리가 뭐로 나왔든, 점수가 몇 점이든 우선 적용.
technique_visible = gpt_result.get("barber_technique_visible", False)
evidence = (gpt_result.get("technique_evidence") or "").strip()

# 근거 강제: visible=true인데 근거 문장이 비어있으면 신뢰 불가 → visible=false로 강등
if technique_visible and not evidence:
    technique_visible = False

# shop_atmosphere(사람 없는 매장 인테리어)는 시술이 안 보이는 게 정상 → 게이트 예외
if photo_category != "shop_atmosphere" and not technique_visible:
    instant_fail = True
```
- **shop_atmosphere 예외 필수** (매장 인테리어는 사람/시술이 없어 정상).
- 기존 `if photo_category in ("irrelevant","other_service"): instant_fail=True` 는 유지
  (수정 A에서 정의가 명확해진 상태).

## 변경 3 — barber_portrait의 gradient·model_vibe 이중 제외 재검토
현재 barber_portrait은 gradient·model_vibe **둘 다** instant_fail 제외. model_vibe 제외
사유는 "뒷면 사진 보호"였는데, 뒷면 사진은 gradient가 잘 나오는 경우라 model_vibe까지
뺄 근거가 약하다. 위 이진 게이트가 들어오면 이 완화의 위험은 줄지만, 여전히 재검토 대상.
→ **제안**: 이진 게이트 도입 후 5회 재측정에서 barber_portrait 오통과가 남으면
model_vibe를 instant_fail 대상으로 복귀. (베이스라인 비교로 결정, 지금 추측 금지.)

## 변경 4 — 미분석 불변조건 (버그 B 대응)
"분석 안 된 사진은 절대 통과/is_usable 상태가 될 수 없다"를 코드에 명시.
- `_save_pass_result` 진입 전, `analyzed_at`·`scores`가 없으면 pass 저장 거부(예외/스킵).
- 현재 파이프라인엔 이걸 뚫는 라이브 경로는 없음(버그 B 결론)이나, 방어적 불변조건으로 고정.
- ※ 이건 관련성 게이트와 독립. PART 2에 함께 넣되 커밋 메시지에서 별개 관심사로 표기하거나
  필요 시 별도 커밋.

---

## 측정 (동일 5회 프로토콜)
수정 A 배포본을 기준선으로, PART 2 적용 후 동일 조건(rules-only, 5회, temp 기본)으로 재측정.
- **개선 기대**: false pass 감소 (g18~g23 무관 인물). 
- **감시 지점**: 지현 지적대로 이진 게이트는 노이즈 성격을 바꿈 — technique_visible이
  애매한 사진에서 true↔false 뒤집히면 즉시 pass↔fail이 뒤집힘. 그래서 하한선(노이즈)
  **재측정 필수**. 5회 지표5(사진별 안정성)에서 새로 불안정해지는 사진을 확인.
- 개선이 하한선을 넘는지로 판단. 넘으면 배포 후보, 아니면 프롬프트/게이트 재조정.

## 배포
개선 확인 후 별도 `fix/` 커밋. 수정 A와 분리.
