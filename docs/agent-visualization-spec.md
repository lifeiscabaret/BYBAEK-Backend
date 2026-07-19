# 에이전트 시각화 스펙 (백엔드 관점) — 프론트 핸드오프용

| | |
|---|---|
| **작성일** | 2026-07-18 |
| **목적** | "AI가 실제로 일하는 과정"을 화면으로 보여주기 위해, 백엔드가 내놓는(또는 낼 수 있는) 신호를 정리 |
| **용도** | 이 문서를 근거로 프론트와 UI/인터랙션 설계 |
| **핵심 결론** | 임팩트 큰 데이터 대부분이 **이미 파이프라인 끝 `final_state`에 존재** → 응답 dict에 필드만 추가하면 노출됨(저리스크) |

---

## 0. 신호등 범례

- 🟢 **지금 API 응답에 이미 있음** — 백엔드 변경 0. 프론트가 바로 씀.
- 🟡 **데이터는 `final_state`에 있으나 응답에 미노출** — `run_pipeline` 반환 dict에 필드 추가만 하면 됨(작은 변경).
- 🔴 **구조 변경 필요** — 새 엔드포인트/실시간 스트리밍 등. 데모 후.

---

## 1. 프론트가 지금 받는 것 (baseline)

`POST /api/agent/run` 응답 (`orchestrator_v2.py:290-303`):
```json
{
  "post_id": "...", "caption": "...", "hashtags": ["..."],
  "photo_urls": ["..."], "cta": "...", "status": "draft",
  "quality": { "trend_score": 0.8, "caption_score": 0.84, "retries": 1, "model_used": "full" }
}
```
→ 캡션·해시태그·CTA·사진, 그리고 **품질 점수(트렌드/캡션/재시도/모델티어)** 는 **이미 노출됨.**

---

## 2. AI 작동 과정 (STEP 0–6) — 데모 영상의 핵심 비주얼

로그 실측 기반 단계·소요시간. 타이머 기반 스테퍼의 근거값으로 사용.

| STEP | 이름 | 하는 일 | 실측 소요 | 시각화 포인트 | 준비도 |
|---|---|---|---|---|---|
| 0 | 복잡도 분류 | trigger/사진수로 모델 티어 결정 | <1s | "mini vs full 지능적 선택" | 🟢 `model_used` |
| 1 | 트렌드 수집 | Tavily 실시간 웹서치(날씨·헤어트렌드·경쟁샵) | 캐시히트 ~1s / 미스 ~35s | "실시간 트렌드 리서치 중" + 출처 | 🟡 trend_data |
| 1.5 | 성과 분석 | 과거 게시물 점수 패턴 추출 | ~1s | (내부) | 🔴 |
| 2 | 트렌드 품질 게이트 | LLM이 트렌드 품질 0~1 채점, <0.7면 재검색 | ~5s (+재시도 40~70s) | "AI가 자료 품질 검증" | 🟢 `trend_score` |
| 3 | 사진 선택 | 원장님 조합(페이드2+스타일1+분위기1), 14일 쿨다운 | ~5~15s | "왜 이 4장인지" 각도·점수 | 🟡 selected_photos |
| 4 | RAG(말투 검색) | 벡터DB에서 이 샵 과거 말투 검색·압축 | ~15s | "당신 브랜드 말투 학습" | 🟡 rag_context |
| 5 | 캡션 생성 + 자가평가 | Claude 생성 → GPT가 5항목 채점, <0.7면 재생성/모델승격 | ~20~45s | "AI가 스스로 채점·재작성" | 🟢 `caption_score`,`retries` |
| 6 | 발행 | 검토 분기 → 인스타 카루셀 업로드 | ~20~60s | "발행 완료" | 🟢 `status` |

---

## 3. 시각화 요소 — 임팩트 × 준비도 순

### Tier A — 지금 데이터로 바로 가능 (🟢 백엔드 변경 0)
데모 영상은 이 티어 + 타이머 스테퍼로 충분.

1. **품질 스코어카드** — `trend_score`, `caption_score`, 0.7 임계선, `retries`, mini→full 승격. → "AI가 자기 결과물을 스스로 채점하고 미달이면 다시 만든다"는 핵심 차별점을 한 화면에.
2. **모델 티어 라우팅** — `model_used`(mini/full). → 비용 효율 지능.
3. **최종 산출물 리빌** — caption + hashtags + cta + photos를 타이핑/페이드 효과로.
4. **단계 진행 스테퍼(타이머 기반)** — §2의 소요시간 상수로 STEP 0~6 순차 표시, API 응답 오면 마지막 단계로 스냅.

### Tier B — 작은 백엔드 추가로 임팩트 급상승 (🟡 응답에 필드 추가)
**모두 `final_state`에 이미 존재. `run_pipeline` 반환 dict에 `trace`/`insights` 객체 하나 추가하면 끝(저리스크).**

5. **트렌드 리서치 근거** — `trend_data.trend_sources`, `weather`, `competitor_insights`, `sources_summary`. → "실시간으로 이 출처들을 조사해 반영했다"(신뢰도 킬러 비주얼).
6. **사진 선택 근거** — `selected_photos[*].scores`(fade/styling/vibe), `detected_angle`, 조합 패턴. → "AI가 이 4장을 고른 이유"를 점수 바/각도 태그로.
7. **RAG 검색 결과** — `rag_context.examples`, `tone_rules`, `source`. → "당신 과거 글에서 말투를 학습".
8. **할루시네이션 가드** — `post_writer._validate_and_clean`이 잡은 금칙/과장/지어냄 패턴. → "안전장치가 걸러냄"(현재 응답 미노출, 노출하려면 검증 결과를 state로 올려야 함 — Tier B 중 유일하게 약간 더 손감).

### Tier C — 구조 변경 필요 (🔴 데모 후)
9. **진짜 실시간 진행 스트리밍(SSE/WS)** — orchestrator 각 노드에서 진행 이벤트 방출 + 스트리밍 엔드포인트. 동기 파이프라인 구조를 손대야 함. 데모엔 타이머 기반(Tier A #4)으로 충분하므로 후순위.

---

## 4. 프론트 핸드오프 권장

| 시점 | 범위 | 백엔드 작업 |
|---|---|---|
| **데모 영상** | Tier A 전부 + 타이머 스테퍼 | **없음** (리스크 0) |
| **데모 후 정식 UI** | Tier B(#5~#7) 노출 | `run_pipeline` 반환에 `insights` 객체 추가 (반나절 규모, 저리스크). #8은 검증결과 state화 추가. |
| **추후** | Tier C 실시간 스트리밍 | 세션/인증 재설계와 함께 후순위 |

### Tier B 노출 방법 (참고 — 백엔드 1곳)
`orchestrator_v2.py:290`의 반환 dict에 아래를 덧붙이면 됨(데이터는 이미 `final_state`에 있음):
```python
"insights": {
    "trend": {
        "sources": final_state["trend_data"].get("sources_summary", []),
        "weather": final_state["trend_data"].get("weather", ""),
        "competitor": final_state["trend_data"].get("competitor_insights", {}),
    },
    "photos": [
        {"angle": p.get("detected_angle"), "scores": p.get("scores", {})}
        for p in final_state["selected_photos"]
    ],
    "rag": {
        "examples": final_state["rag_context"].get("examples", []),
        "tone_rules": final_state["rag_context"].get("tone_rules", []),
        "source": final_state["rag_context"].get("source", ""),
    },
    "trend_retries": final_state.get("trend_retries", 0),
}
```
→ 기존 필드는 그대로, 추가만 하므로 프론트/기존 동작에 영향 없음.

---

## 5. 요약 (프론트와 논의 시작점)

- **데모 영상**: Tier A(품질 스코어카드 · 모델티어 · 산출물 리빌 · 타이머 스테퍼)만으로 "AI가 일하는 과정" 임팩트 확보 — **백엔드 변경 0**.
- **가장 가성비 높은 다음 스텝**: Tier B — 트렌드 근거 / 사진 선택 이유 / RAG 학습을 노출. **데이터가 이미 있어서 반환 dict 한 곳만 수정**하면 화면이 극적으로 풍부해짐.
- **후순위**: 실시간 스트리밍은 구조 변경이라 데모 후.
