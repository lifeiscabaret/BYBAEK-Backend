"""
workers/insta_token_refresh.py — Instagram 장기 액세스 토큰 자동 갱신

배경:
  로그인 시 ig_exchange_token으로 받는 장기 토큰은 **60일 만료**다.
  기존엔 insta_expires_in을 저장만 하고 갱신 호출이 코드베이스 어디에도 없어서,
  연동 60일 후 전 샵의 발행/인사이트 수집이 조용히 중단되는 구조였다.

IG 정책:
  - 갱신 엔드포인트: GET graph.instagram.com/refresh_access_token
      grant_type=ig_refresh_token & access_token=<현재 장기 토큰>
  - 발급 후 **24시간이 지나야** 갱신 가능
  - **이미 만료된 토큰은 갱신 불가** → 사장님이 직접 재연동해야 함
  → 만료 직전이 아니라 여유(REFRESH_WINDOW_DAYS)를 두고 미리 돌린다.

실행: main.py lifespan에서 APScheduler로 하루 1회.
멱등하므로 중복 실행돼도 안전하다(갱신될 때마다 만료가 60일로 리셋될 뿐).
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from services.cosmos_db import get_shops_with_instagram, save_auth

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

# 만료 이 일수 이내로 남았을 때만 갱신 (매일 전량 갱신하면 불필요한 API 호출)
REFRESH_WINDOW_DAYS = int(os.getenv("INSTA_TOKEN_REFRESH_WINDOW_DAYS", "20"))
# IG 정책상 발급 24시간 내 토큰은 갱신 불가 → 여유 두고 25시간
MIN_TOKEN_AGE_HOURS = 25
# 동시 갱신 수 (샵이 늘어도 Graph rate limit 안전하게)
MAX_CONCURRENCY = int(os.getenv("INSTA_TOKEN_REFRESH_CONCURRENCY", "5"))


def _parse_iso(value) -> datetime | None:
    """ISO 문자열 → aware datetime. 파싱 실패/빈 값이면 None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 기존 데이터는 datetime.utcnow().isoformat()으로 저장돼 tz가 없다 → UTC로 간주
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_expires_at(shop: dict) -> datetime | None:
    """이 샵 토큰의 만료 시각 추정.

    1) insta_token_expires_at (이 워커/로그인 핸들러가 기록한 절대 시각) 우선
    2) 없으면 insta_updated_at + 60일로 추정 (갱신 도입 이전에 연동한 샵)
    3) 둘 다 없으면 None → 만료 시점 불명이므로 일단 갱신 시도
    """
    explicit = _parse_iso(shop.get("insta_token_expires_at"))
    if explicit:
        return explicit

    issued = _parse_iso(shop.get("insta_updated_at"))
    if issued:
        return issued + timedelta(days=60)

    return None


def _token_age_ok(shop: dict, now: datetime) -> bool:
    """발급/갱신 후 24시간이 안 지난 토큰은 IG가 갱신을 거부한다."""
    issued = _parse_iso(shop.get("insta_updated_at"))
    if not issued:
        return True  # 발급 시각 불명 → 시도해보고 에러로 판단
    return (now - issued) >= timedelta(hours=MIN_TOKEN_AGE_HOURS)


async def _refresh_one(shop: dict, client: httpx.AsyncClient, now: datetime) -> str:
    """샵 하나의 토큰 갱신. 반환값은 집계용 결과 코드."""
    shop_id = shop.get("id") or shop.get("shop_id")
    token = shop.get("insta_access_token")
    if not shop_id or not token:
        return "skipped_no_token"

    expires_at = _resolve_expires_at(shop)

    if expires_at and expires_at <= now:
        # 이미 만료 — 갱신 불가. 사장님 재연동이 필요하다는 플래그만 남긴다.
        print(f"[insta_token] {shop_id} 토큰 만료됨({expires_at.isoformat()}) → 재연동 필요")
        await asyncio.to_thread(save_auth, shop_id, {"insta_token_needs_reconnect": True})
        return "expired"

    if expires_at and (expires_at - now) > timedelta(days=REFRESH_WINDOW_DAYS):
        return "skipped_not_due"

    if not _token_age_ok(shop, now):
        return "skipped_too_young"

    try:
        resp = await client.get(
            REFRESH_URL,
            params={"grant_type": "ig_refresh_token", "access_token": token},
            timeout=20,
        )
        body = resp.json() if resp.content else {}
    except Exception as e:
        print(f"[insta_token] {shop_id} 갱신 요청 실패: {e}")
        return "error"

    if resp.status_code != 200 or "access_token" not in body:
        print(f"[insta_token] {shop_id} 갱신 거부 (status={resp.status_code}): {body}")
        return "error"

    new_token = body["access_token"]
    expires_in = int(body.get("expires_in", 60 * 24 * 3600))
    new_expires_at = now + timedelta(seconds=expires_in)

    await asyncio.to_thread(save_auth, shop_id, {
        "insta_access_token":          new_token,
        "insta_expires_in":            expires_in,
        "insta_token_expires_at":      new_expires_at.isoformat(),
        "insta_updated_at":            now.isoformat(),
        "insta_token_needs_reconnect": False,
    })
    print(f"[insta_token] {shop_id} 갱신 완료 → 만료 {new_expires_at.date().isoformat()}")
    return "refreshed"


async def refresh_all_instagram_tokens() -> dict:
    """인스타 연동된 전 샵의 장기 토큰을 만료 전에 갱신한다."""
    now = datetime.now(timezone.utc)

    try:
        shops = await asyncio.to_thread(get_shops_with_instagram)
    except Exception as e:
        print(f"[insta_token] 샵 목록 조회 실패: {e}")
        return {}

    if not shops:
        print("[insta_token] 인스타 연동 샵 없음 → 스킵")
        return {}

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with httpx.AsyncClient() as client:
        async def guarded(shop):
            async with sem:
                try:
                    return await _refresh_one(shop, client, now)
                except Exception as e:
                    print(f"[insta_token] 예외 (무시): {e}")
                    return "error"

        results = await asyncio.gather(*(guarded(s) for s in shops))

    summary: dict = {}
    for r in results:
        summary[r] = summary.get(r, 0) + 1
    print(f"[insta_token] 완료 → 대상 {len(shops)}개 샵, 결과 {summary}")
    return summary
