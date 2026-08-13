"""
파일명: auth/token_verify.py
역할: 백엔드 자체 발급(HS256) 액세스 토큰의 발급/검증 및 FastAPI 인증 의존성.

[배경]
- 프론트엔드는 클라이언트 MSAL이 없고, 로그인은 Easy Auth 서버측 팝업(/.auth/login/aad)으로 처리된다.
- 그래서 Azure AD 토큰을 백엔드가 JWKS로 검증하는 방식 대신,
  /auth/me 가 Easy Auth 헤더로 신원 확인에 성공하는 시점에 백엔드가
  자체 서명 JWT를 발급하고, 이후 모든 API 호출은 이 토큰을 Bearer로 사용한다.
- 검증은 외부 JWKS 조회 없이 백엔드 비밀키(JWT_SECRET)만으로 수행한다.

[보안 원칙]
- 실패는 실패로. 인증 헤더 없음/서명 불일치/만료 → 반드시 401. 기본 계정 폴백 없음.
- shop_id 는 오직 "서명이 검증된 토큰 payload" 에서만 취한다 (경로/바디의 shop_id 는 신뢰하지 않음).
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import Request, HTTPException
from jose import jwt, JWTError

from services.cosmos_db import get_auth
from utils.logging import logger

JWT_ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 24

# 이메일 검토 링크 전용 토큰. 초안의 review_deadline(24h)보다 넉넉히 잡아
# "메일은 왔는데 링크는 이미 죽어있는" 상황을 피한다.
REVIEW_TOKEN_TTL_HOURS = 48
REVIEW_TOKEN_HEADER = "X-Review-Token"


def _get_secret() -> str:
    """JWT_SECRET 환경변수. 미설정이면 서버 설정 오류로 500 (기본값 폴백 금지)."""
    secret = os.getenv("JWT_SECRET")
    if not secret:
        logger.error("[auth] JWT_SECRET 미설정 — 토큰 발급/검증 불가")
        raise HTTPException(status_code=500, detail="서버 인증 설정 오류")
    return secret


def issue_token(shop_id: str) -> str:
    """로그인 신원 확인 직후 호출. shop_id 를 담은 24h 만료 JWT 발급."""
    now = datetime.now(timezone.utc)
    payload = {
        "shop_id": shop_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _get_secret(), algorithm=JWT_ALGORITHM)


def sign_short_lived(payload: dict, ttl_seconds: int) -> str:
    """임의의 dict를 짧은 수명 JWT로 서명. OAuth flow 상태를 쿠키에 안전하게 담을 때 사용."""
    now = datetime.now(timezone.utc)
    body = {
        "data": payload,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(body, _get_secret(), algorithm=JWT_ALGORITHM)


def verify_short_lived(token: str) -> dict:
    """sign_short_lived로 만든 토큰을 검증하고 원래 dict를 반환. 실패 시 JWTError."""
    body = jwt.decode(token, _get_secret(), algorithms=[JWT_ALGORITHM])
    return body.get("data") or {}


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다")
    token = header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다")
    return token


async def get_current_shop(request: Request) -> dict:
    """Authorization 헤더의 백엔드 자체 발급 토큰을 검증하고 해당 Shop 문서를 반환.

    실패(헤더 없음/서명 불일치/만료/shop_id 없음) 시 HTTPException(401) — 폴백 없음.
    반환 dict 의 shop_id 는 항상 '서명이 검증된 토큰' 의 값이다.
    """
    token = _extract_bearer(request)
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="유효하지 않거나 만료된 토큰입니다")

    shop_id = payload.get("shop_id")
    if not shop_id:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")

    # 서명이 확인된 시점에서 신원은 이미 증명됨. Shop 문서는 부가 정보로 조회한다.
    # Cosmos 일시 장애 등으로 문서 조회가 실패해도 (get_auth 는 None 반환),
    # 소유권 검증에 필요한 shop_id 는 서명된 토큰에서 나오므로 최소 문서를 돌려준다.
    shop = get_auth(shop_id)
    if shop is None:
        logger.warning(f"[auth] 유효 토큰이나 Shop 문서 조회 불가: {shop_id}")
        return {"shop_id": shop_id}

    shop["shop_id"] = shop_id  # 신뢰의 원천을 토큰 값으로 고정
    return shop


def require_shop_owner(current_shop: dict, shop_id: str) -> None:
    """경로/바디의 shop_id 가 토큰에서 확인된 본인 샵과 일치하는지 검증. 불일치 시 403.

    IDOR 방어의 핵심: 로그인 여부만이 아니라 '요청 대상이 본인 것인지' 를 매번 확인한다.
    """
    if current_shop.get("shop_id") != shop_id:
        raise HTTPException(status_code=403, detail="다른 샵의 데이터에 접근할 수 없습니다")


def issue_review_token(shop_id: str, post_id: str) -> str:
    """이메일 검토 알림 링크에 실을 '초안 1건 전용' 토큰.

    [왜 필요한가]
    프론트는 세션 access_token 을 sessionStorage 에 둔다. sessionStorage 는 탭 단위라
    메일 앱에서 눌러 새로 열린 탭에는 토큰이 없다 → 검토 링크는 사실상 항상 401 이었다.

    [왜 세션 토큰을 그냥 주지 않는가]
    메일 링크가 새어나가면 샵 전체 권한이 넘어간다. 그래서 sign_short_lived 로
    {"data": {...}} 형태로 감싸 서명한다. 이 토큰은 top-level 에 shop_id 가 없으므로
    get_current_shop() 으로는 절대 통과하지 못하고, 오직 아래 get_current_shop_or_review()
    를 쓰는 엔드포인트에서 payload 의 post_id 하나에 대해서만 통한다.
    """
    return sign_short_lived(
        {"scope": "review", "shop_id": shop_id, "post_id": post_id},
        ttl_seconds=REVIEW_TOKEN_TTL_HOURS * 3600,
    )


async def get_current_shop_or_review(request: Request) -> dict:
    """세션 토큰(Authorization) 우선, 없으면 검토 전용 토큰(X-Review-Token)을 받는다.

    검토 토큰으로 통과한 경우 반환 dict 에 review_post_id 가 담긴다.
    이 값이 있으면 반드시 require_post_access() 로 대상 post_id 를 함께 검증해야 한다.
    (require_shop_owner 만 쓰면 검토 토큰이 샵 전체 권한처럼 동작해버린다.)
    """
    if request.headers.get("Authorization") or request.headers.get("authorization"):
        return await get_current_shop(request)

    token = request.headers.get(REVIEW_TOKEN_HEADER)
    if not token:
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다")

    try:
        data = verify_short_lived(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="만료되었거나 유효하지 않은 검토 링크입니다")

    if data.get("scope") != "review" or not data.get("shop_id") or not data.get("post_id"):
        raise HTTPException(status_code=401, detail="유효하지 않은 검토 링크입니다")

    return {"shop_id": data["shop_id"], "review_post_id": data["post_id"]}


def require_post_access(current_shop: dict, shop_id: str, post_id: str) -> None:
    """require_shop_owner + 검토 전용 토큰이면 링크에 서명된 post_id 로 범위를 제한한다."""
    require_shop_owner(current_shop, shop_id)
    review_post_id = current_shop.get("review_post_id")
    if review_post_id is not None and review_post_id != post_id:
        raise HTTPException(status_code=403, detail="이 검토 링크로는 접근할 수 없는 게시물입니다")
