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


async def node_fetch_performance(state: dict) -> dict:
    """
    STEP 1.5: 최근 게시물 성과 데이터 분석 노드.
    fetch_data 이후, evaluate_trend 이전에 실행.

    caption_score 기록 기반으로 성공/실패 패턴 추출.
    인스타 실제 반응(좋아요/DM) 데이터가 나중에 연동되면
    이 함수만 수정하면 자동으로 고도화됨.
    """
    shop_id = state["shop_id"]
    print(f"[performance] 성과 분석 시작 → shop_id={shop_id}")

    try:
        from services.cosmos_db import get_recent_drafts_with_scores
        drafts = get_recent_drafts_with_scores(shop_id, limit=20)
    except Exception as e:
        print(f"[performance] 성과 데이터 로드 실패 ({e}) → 빈 프로파일")
        return {**state, "performance_history": _empty_profile()}

    if not drafts or len(drafts) < 3:
        print(f"[performance] 데이터 부족 ({len(drafts)}개) → 빈 프로파일")
        return {**state, "performance_history": _empty_profile()}

    profile = await _analyze_performance(drafts)
    print(f"[performance] 분석 완료 → best_score_avg={profile['best_patterns']['best_score_avg']:.2f}")
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


async def _analyze_performance(drafts: list) -> dict:
    """
    저장된 게시물 + caption_score 기반 패턴 분석.
    상위 30% vs 하위 30% 비교.
    """
    # caption_score 기준 정렬
    scored = [d for d in drafts if d.get("caption_score") is not None]
    scored.sort(key=lambda x: x["caption_score"], reverse=True)

    if len(scored) < 3:
        return _empty_profile()

    top_count    = max(1, len(scored) // 3)
    bottom_count = max(1, len(scored) // 3)

    top_posts    = scored[:top_count]
    bottom_posts = scored[-bottom_count:]

    best_patterns  = _extract_patterns(top_posts)
    worst_patterns = _extract_patterns(bottom_posts)

    best_patterns["best_score_avg"]   = round(
        sum(p["caption_score"] for p in top_posts) / len(top_posts), 2
    )
    worst_patterns["worst_score_avg"] = round(
        sum(p["caption_score"] for p in bottom_posts) / len(bottom_posts), 2
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
