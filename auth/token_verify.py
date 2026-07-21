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
