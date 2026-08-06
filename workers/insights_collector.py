"""
workers/insights_collector.py — 발행 게시물의 실제 반응 수집

배경:
  성과 피드백이 지금까지 참고한 건 _evaluate_caption()의 자기평가 점수(caption_score)뿐이었다.
  "AI가 자기 글에 점수 매기고 잘했다고 판단"하는 구조라, 실제 마케팅 효과와 무관하다.
  → 인스타 실참여(좋아요/댓글/도달/저장)를 발행 후 시점별로 수집해 둔다.

수집 시점:
  24h — 초기 확산 (대부분의 반응이 이 구간에 나온다)
  7d  — 롱테일 (탐색탭/해시태그 유입)
  두 창을 나눠 두면 나중에 "초기 훅이 좋았는지" vs "검색 유입이 좋았는지"를 구분할 수 있다.

주의:
  - 이 워커는 **수집만** 한다. 수집된 지표를 캡션 생성에 반영하는 자동 피드백 루프는
    performance_feedback.py 쪽 게이트가 따로 통제한다 (현재 보류 상태).
  - 스코프: instagram_business_manage_insights (2026-08-05 실측으로 동의 범위 확인됨)
  - impressions는 폐기됨 → views 사용
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from services.cosmos_db import (get_auth, get_posts_for_engagement,
                                save_post_engagement)

GRAPH_BASE = "https://graph.instagram.com/v25.0"

# 발행 후 이 시간이 지나면 해당 창을 수집한다
WINDOWS = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
}

# 미디어 타입에 따라 지원되지 않는 지표가 있어, 실패 시 개별 호출로 폴백한다
INSIGHT_METRICS = ["reach", "saved", "views", "shares", "total_interactions"]

# Graph rate limit 보호 — 한 번 실행에서 처리할 최대 (게시물 × 창) 수.
# 기존 발행분 수십 건이 첫 실행에 몰리므로 상한을 두고 여러 번에 나눠 처리한다.
MAX_PER_RUN = int(os.getenv("INSIGHTS_MAX_PER_RUN", "25"))
MAX_CONCURRENCY = int(os.getenv("INSIGHTS_CONCURRENCY", "4"))


def _parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _published_at(post: dict) -> datetime | None:
    """발행 시각. published_at은 2026-08 이후 발행분에만 있어 created_at으로 폴백한다."""
    return _parse_iso(post.get("published_at")) or _parse_iso(post.get("created_at"))


def _due_windows(post: dict, now: datetime) -> list:
    """이 게시물에서 아직 수집 안 됐고 시점이 지난 창 목록."""
    published = _published_at(post)
    if not published:
        return []
    collected = (post.get("engagement") or {})
    due = []
    for name, delta in WINDOWS.items():
        if name in collected:
            continue
        if now - published >= delta:
            due.append(name)
    return due


async def _fetch_media_fields(client, media_id, token) -> dict:
    """좋아요/댓글 수 — insights 스코프 없이도 되는 기본 필드."""
    r = await client.get(f"{GRAPH_BASE}/{media_id}",
                         params={"fields": "media_type,timestamp,like_count,comments_count",
                                 "access_token": token})
    if r.status_code != 200:
        print(f"[insights] media 조회 실패 {media_id}: HTTP {r.status_code} {r.text[:160]}")
        return {}
    return r.json()


async def _fetch_insights(client, media_id, token) -> dict:
    """도달/저장/조회/공유/총반응. 미디어 타입별로 지원 지표가 달라 실패 시 개별 재시도."""
    async def call(metrics):
        return await client.get(f"{GRAPH_BASE}/{media_id}/insights",
                                params={"metric": ",".join(metrics), "access_token": token})

    def unpack(body):
        out = {}
        for row in body.get("data", []):
            values = row.get("values") or [{}]
            out[row.get("name")] = values[0].get("value")
        return out

    r = await call(INSIGHT_METRICS)
    if r.status_code == 200:
        return unpack(r.json())

    # 일괄 실패 → 지표 하나씩 시도해서 되는 것만 건진다
    result = {}
    for metric in INSIGHT_METRICS:
        rr = await call([metric])
        if rr.status_code == 200:
            result.update(unpack(rr.json()))
    if not result:
        print(f"[insights] insights 조회 실패 {media_id}: HTTP {r.status_code} {r.text[:160]}")
    return result


# 창 시점보다 이 배수 이상 늦게 수집했으면 "그 시점의 값"이 아니다 → backfilled 표시.
# (워커 도입 전에 발행된 게시물은 24h 창을 몇 달 뒤에 재게 되므로 구분이 필요하다)
BACKFILL_TOLERANCE = 1.5


async def _collect_one(post, window, token, client, now) -> bool:
    media_id = str(post["instagram_media_id"])
    shop_id = post["shop_id"]

    fields, insights = await asyncio.gather(
        _fetch_media_fields(client, media_id, token),
        _fetch_insights(client, media_id, token),
    )
    if not fields and not insights:
        return False

    published = _published_at(post)
    age = (now - published) if published else None
    backfilled = bool(age and age > WINDOWS[window] * BACKFILL_TOLERANCE)

    snapshot = {
        "collected_at":       now.isoformat(),
        "window":             window,
        # True면 창 시점이 한참 지난 뒤 잰 값 — 시점별 비교(24h vs 7d)에 쓰면 안 된다
        "backfilled":         backfilled,
        "age_hours_at_collect": round(age.total_seconds() / 3600, 1) if age else None,
        "like_count":         fields.get("like_count"),
        "comments_count":     fields.get("comments_count"),
        "media_type":         fields.get("media_type"),
        "reach":              insights.get("reach"),
        "saved":              insights.get("saved"),
        "views":              insights.get("views"),
        "shares":             insights.get("shares"),
        "total_interactions": insights.get("total_interactions"),
    }
    ok = await asyncio.to_thread(save_post_engagement, shop_id, post["id"], window, snapshot)
    if ok:
        print(f"[insights] {post['id']} [{window}] ♥{snapshot['like_count']} "
              f"💬{snapshot['comments_count']} reach={snapshot['reach']} "
              f"saved={snapshot['saved']}")
    return ok


async def collect_all_engagement() -> dict:
    """발행 후 24h/7d가 지난 게시물의 실참여를 수집한다. 스케줄러 진입점."""
    now = datetime.now(timezone.utc)

    try:
        posts = await asyncio.to_thread(get_posts_for_engagement)
    except Exception as e:
        print(f"[insights] 대상 조회 실패: {e}")
        return {}

    jobs = []
    for post in posts:
        for window in _due_windows(post, now):
            jobs.append((post, window))

    if not jobs:
        print("[insights] 수집 대상 없음")
        return {"pending": 0}

    total_pending = len(jobs)
    if len(jobs) > MAX_PER_RUN:
        print(f"[insights] 대상 {total_pending}건 → 이번 실행은 {MAX_PER_RUN}건만 처리 "
              f"(나머지는 다음 실행에서)")
        jobs = jobs[:MAX_PER_RUN]

    # 샵별 토큰은 한 번만 조회
    tokens = {}
    for post, _ in jobs:
        sid = post["shop_id"]
        if sid not in tokens:
            auth = await asyncio.to_thread(get_auth, sid) or {}
            tokens[sid] = auth.get("insta_access_token")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    ok = skipped = failed = 0

    async with httpx.AsyncClient(timeout=30) as client:
        async def guarded(post, window):
            nonlocal ok, skipped, failed
            token = tokens.get(post["shop_id"])
            if not token:
                skipped += 1
                return
            async with sem:
                try:
                    if await _collect_one(post, window, token, client, now):
                        ok += 1
                    else:
                        failed += 1
                except Exception as e:
                    print(f"[insights] 수집 예외 ({post['id']}/{window}): {e}")
                    failed += 1

        await asyncio.gather(*(guarded(p, w) for p, w in jobs))

    summary = {"collected": ok, "failed": failed, "skipped_no_token": skipped,
               "pending_next_run": max(0, total_pending - len(jobs))}
    print(f"[insights] 완료 → {summary}")
    return summary
