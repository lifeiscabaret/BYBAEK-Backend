"""
orchestrator_v2.py에 추가할 성과 피드백 루프 코드

PostState에 performance_history 키 추가 필요:
class PostState(TypedDict):
    ...기존 키들...
    performance_history: dict   # ← 추가

흐름:
fetch_data → [fetch_performance] → evaluate_trend → ...
                    ↓
          성과 좋았던 패턴을 RAG context에 주입
          → post_writer가 과거 성공 패턴 참고해서 작성
"""

import json
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


# ──────────────────────────────────────────
# PostState에 추가할 키
# ──────────────────────────────────────────
# performance_history: dict
# 예시:
# {
#   "best_patterns": {
#     "keywords": ["직장인", "출근룩"],     # 성과 좋은 캡션에 자주 나온 키워드
#     "emoji": ["✂️", "💈"],              # 성과 좋은 게시물에 쓰인 이모지
#     "caption_length": "short",           # 짧은 캡션이 더 잘 됐는지
#     "best_score_avg": 0.84
#   },
#   "worst_patterns": {
#     "keywords": ["마감 임박", "한정"],
#     "worst_score_avg": 0.41
#   },
#   "total_posts_analyzed": 12
# }


# ──────────────────────────────────────────
# 자동 피드백 게이트
#
# 예전 조건: 초안이 3개만 있어도 caption_score(자기평가) 상하위로 패턴을 뽑았다.
#   → AI가 자기 점수로 자기 패턴을 강화하는 루프. 실제 마케팅 효과와 무관.
#
# 바뀐 조건: 실참여(insights_collector가 수집)에 **의미 있는 분산**이 생긴 뒤에만 켠다.
#   표본 개수만으로 여는 건 틀린 기준이다 — 모든 게시물의 반응이 0이면
#   300개를 모아도 학습할 신호가 없다 (실측 2026-08: 게시물 200개, 좋아요 총합 6,
#   팔로워 1명 → 분산 0).
#   그래서 아래 세 조건을 모두 만족해야 패턴 추출을 시작한다.
# ──────────────────────────────────────────

# 통계를 내려면 최소한 필요한 표본 (분산 조건의 전제일 뿐, 이것만으론 열리지 않는다)
MIN_SAMPLES = 10
# 반응이 0이 아닌 게시물이 최소 이만큼은 있어야 한다
MIN_NONZERO = 5
# 상위 그룹 평균이 하위 그룹 평균의 몇 배 이상이어야 "차이가 있다"고 볼지
MIN_SPREAD_RATIO = 2.0


def _engagement_value(post: dict) -> float | None:
    """이 게시물의 대표 참여 지표. 24h 창을 우선 쓰고 없으면 7d.

    total_interactions(좋아요+저장+댓글+공유)를 기본으로 하고,
    없으면 좋아요+댓글로 폴백한다.
    """
    eng = post.get("engagement") or {}
    snap = eng.get("24h") or eng.get("7d")
    if not snap:
        return None
    total = snap.get("total_interactions")
    if total is not None:
        return float(total)
    likes = snap.get("like_count") or 0
    comments = snap.get("comments_count") or 0
    return float(likes + comments)


def _variance_gate(values: list) -> tuple:
    """자동 피드백을 열어도 되는지 판정. (통과여부, 사유) 반환."""
    n = len(values)
    if n < MIN_SAMPLES:
        return False, f"표본 부족 ({n}/{MIN_SAMPLES})"

    nonzero = [v for v in values if v > 0]
    if len(nonzero) < MIN_NONZERO:
        return False, f"반응 있는 게시물 부족 ({len(nonzero)}/{MIN_NONZERO}) — 분산 없음"

    ordered = sorted(values, reverse=True)
    k = max(1, n // 3)
    top_avg = sum(ordered[:k]) / k
    bottom_avg = sum(ordered[-k:]) / k

    if bottom_avg <= 0:
        # 하위가 전부 0이면 비율이 무한대가 되므로 상위 절대값으로 판단
        if top_avg < MIN_NONZERO:
            return False, f"상하위 차이가 노이즈 수준 (상위평균 {top_avg:.1f})"
        return True, f"상위평균 {top_avg:.1f} / 하위평균 0"

    ratio = top_avg / bottom_avg
    if ratio < MIN_SPREAD_RATIO:
        return False, f"상하위 차이 부족 (배율 {ratio:.2f} < {MIN_SPREAD_RATIO})"
    return True, f"상위평균 {top_avg:.1f} / 하위평균 {bottom_avg:.1f} (배율 {ratio:.2f})"


async def node_fetch_performance(state: dict) -> dict:
    """
    STEP 1.5: 최근 게시물 성과 분석 노드.
    fetch_data 이후, evaluate_trend 이전에 실행.

    **실참여 기준**으로 성공/실패 패턴을 추출한다.
    분산 게이트를 통과하지 못하면 빈 프로파일을 반환한다 (= 자동 피드백 미적용).
    """
    shop_id = state["shop_id"]
    print(f"[performance] 성과 분석 시작 → shop_id={shop_id}")

    try:
        from services.cosmos_db import get_posts_with_engagement
        posts = get_posts_with_engagement(shop_id, limit=50)
    except Exception as e:
        print(f"[performance] 성과 데이터 로드 실패 ({e}) → 빈 프로파일")
        return {**state, "performance_history": _empty_profile()}

    scored = []
    for p in posts:
        v = _engagement_value(p)
        if v is not None:
            scored.append({**p, "engagement_value": v})

    passed, reason = _variance_gate([p["engagement_value"] for p in scored])
    if not passed:
        print(f"[performance] 자동 피드백 보류 → {reason} "
              f"(수집된 게시물 {len(scored)}개)")
        return {**state, "performance_history": _empty_profile()}

    print(f"[performance] 분산 게이트 통과 → {reason}")
    profile = await _analyze_performance(scored)
    print(f"[performance] 분석 완료 → best_engagement_avg={profile['best_patterns']['best_score_avg']:.2f}")
    return {**state, "performance_history": profile}


async def inject_performance_to_rag(
    rag_context: dict,
    performance_history: dict
) -> dict:
    """
    RAG context에 성과 패턴 주입.
    node_search_rag() 이후, node_write_post() 이전에 호출.

    post_writer가 "과거에 잘 됐던 패턴"을 참고해서 글을 씀.
    """
    if not performance_history or not performance_history.get("best_patterns"):
        return rag_context

    best = performance_history["best_patterns"]
    worst = performance_history.get("worst_patterns", {})

    # RAG context에 성과 인사이트 추가
    performance_note = []

    if best.get("keywords"):
        performance_note.append(
            f"최근 성과 좋은 게시물 키워드: {', '.join(best['keywords'][:3])}"
        )

    if best.get("emoji"):
        performance_note.append(
            f"성과 좋은 게시물에 자주 쓰인 이모지: {' '.join(best['emoji'][:3])}"
        )

    if best.get("caption_length"):
        length_label = "짧은 캡션(2줄 이하)" if best["caption_length"] == "short" else "긴 캡션(4줄 이상)"
        performance_note.append(f"이 샵은 {length_label}이 더 잘 됨")

    if worst.get("keywords"):
        performance_note.append(
            f"피해야 할 표현 (성과 낮았음): {', '.join(worst['keywords'][:2])}"
        )

    if performance_note:
        rag_context["performance_insights"] = "\n".join(performance_note)
        print(f"[performance] RAG에 성과 인사이트 주입 → {len(performance_note)}개")

    return rag_context


async def _analyze_performance(scored: list) -> dict:
    """실참여(engagement_value) 기준 상위/하위 1/3 패턴 비교.

    호출 전에 _variance_gate를 통과했다는 전제 — 여기서 표본/분산을 다시 보지 않는다.
    """
    scored = sorted(scored, key=lambda x: x["engagement_value"], reverse=True)

    k = max(1, len(scored) // 3)
    top_posts    = scored[:k]
    bottom_posts = scored[-k:]

    best_patterns  = _extract_patterns(top_posts)
    worst_patterns = _extract_patterns(bottom_posts)

    best_patterns["best_score_avg"]   = round(
        sum(p["engagement_value"] for p in top_posts) / len(top_posts), 2
    )
    worst_patterns["worst_score_avg"] = round(
        sum(p["engagement_value"] for p in bottom_posts) / len(bottom_posts), 2
    )

    return {
        "best_patterns":        best_patterns,
        "worst_patterns":       worst_patterns,
        "total_posts_analyzed": len(scored)
    }


def _extract_patterns(posts: list) -> dict:
    """캡션에서 키워드, 이모지, 길이 패턴 추출."""
    import re

    keyword_freq = {}
    emoji_freq   = {}
    lengths      = []

    target_keywords = [
        "직장인", "출근", "대학생", "페이드", "투블럭", "스킨",
        "포마드", "슬릭백", "크롭", "리젠트", "사이드파트"
    ]

    emoji_pattern = re.compile(
        "[\U00010000-\U0010ffff"
        "\U0001F300-\U0001F9FF"
        "\u2702-\u27B0]+",
        flags=re.UNICODE
    )

    for post in posts:
        caption = post.get("caption", "")
        if not caption:
            continue

        # 키워드 빈도
        for kw in target_keywords:
            if kw in caption:
                keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

        # 이모지 추출
        emojis = emoji_pattern.findall(caption)
        for e in emojis:
            emoji_freq[e] = emoji_freq.get(e, 0) + 1

        # 길이
        lengths.append(len(caption))

    # 상위 키워드/이모지
    top_keywords = sorted(keyword_freq, key=keyword_freq.get, reverse=True)[:3]
    top_emojis   = sorted(emoji_freq,   key=emoji_freq.get,   reverse=True)[:3]

    # 캡션 길이 경향
    avg_length = sum(lengths) / len(lengths) if lengths else 100
    caption_length = "short" if avg_length < 80 else "long"

    return {
        "keywords":       top_keywords,
        "emoji":          top_emojis,
        "caption_length": caption_length,
    }


def _empty_profile() -> dict:
    return {
        "best_patterns":        {"keywords": [], "emoji": [], "caption_length": "medium", "best_score_avg": 0.0},
        "worst_patterns":       {"keywords": [], "worst_score_avg": 0.0},
        "total_posts_analyzed": 0
    }
