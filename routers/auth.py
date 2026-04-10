from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from utils.logging import logger
import os
import requests
from services.cosmos_db import save_auth, get_auth
import logging
from datetime import datetime
from fastapi.responses import RedirectResponse

router = APIRouter()

class InstagramLoginRequest(BaseModel):
    code: str

@router.get("/ms/callback")
async def ms_callback():
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return RedirectResponse(url=f"{frontend_url}/auth/callback")

@router.post("/instagram", status_code=status.HTTP_201_CREATED)
async def instagram_business_login(req: InstagramLoginRequest, res: Response, fast_req: Request) -> Response:

    access_token = fast_req.headers.get("x-ms-token-aad-access-token")
    logger.info(f"access token = {access_token}")

    code = req.code
    if not code:
        raise HTTPException(status_code=401, detail="authorize code doesnt exist")

    # 1. 단기 토큰 발급
    payload = {
        'client_id':     (None, os.getenv("client_id")),
        'client_secret': (None, os.getenv("client_secret")),
        'grant_type':    (None, "authorization_code"),
        'redirect_uri':  (None, os.getenv("redirect_uri")),
        'code':          (None, code)
    }

    response = requests.post("https://api.instagram.com/oauth/access_token", files=payload)
    response = response.json()

    if 'error' in response:
        logger.error(f'단기 토큰 발급 실패: {response}')
        raise HTTPException(status_code=400, detail=str(response))

    user_id = response.get('user_id') or response.get('id')
    short_access_token = response['access_token']

    # 2. 장기 토큰 교환 (GET 방식 — 인스타그램 API 스펙)
    params = {
        'grant_type':    'ig_exchange_token',
        'client_secret': os.getenv("client_secret"),
        'access_token':  short_access_token
    }

    response = requests.get("https://graph.instagram.com/access_token", params=params)
    response = response.json()

    if 'error' in response:
        logger.error(f'장기 토큰 교환 실패: {response}')
        raise HTTPException(status_code=400, detail=str(response))

    access_token = response['access_token']
    expires_in   = response['expires_in']

    res.set_cookie(
        key="user_id",
        value=user_id,
        httponly=True,
        secure=True,
        samesite="none"
    )

    logger.info(fast_req.cookies)

    ms_id = fast_req.headers.get("X-MS-CLIENT-PRINCIPAL-ID") or "test_barber_jiyeon"

    insta_data = {
        "insta_access_token": access_token,
        "insta_user_id":      user_id,
        "insta_updated_at":   datetime.utcnow().isoformat()
    }
    save_auth(ms_id, insta_data)

    return {'access_token': access_token, "user_id": user_id}


@router.get("/me")
async def get_my_info(request: Request):
    ms_user_id   = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    ms_user_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")

    if not ms_user_id:
        ms_user_id   = "test_barber_jiyeon"
        ms_user_name = "jiyeon@test.com"

    current_time = datetime.utcnow().isoformat()

    existing_user = get_auth(ms_user_id)

    auth_data = {
        "name":          ms_user_name,
        "last_login_at": current_time,
    }

    # ── refresh_token 저장 (추가된 부분) ──
    # Worker가 OneDrive 파일 다운로드 시 토큰 만료 문제 해결용
    # Easy Auth 설정에서 offline_access 스코프 추가 필수
    refresh_token = request.headers.get("x-ms-token-aad-refresh-token")
    if refresh_token:
        auth_data["refresh_token"] = refresh_token

    if not existing_user:
        auth_data["created_at"] = current_time
        logging.info(f"신규 유저 가입: {ms_user_id}")
    else:
        logging.info(f"기존 유저 로그인: {ms_user_id}")

    save_auth(ms_user_id, auth_data)

    return {"shop_id": ms_user_id, "is_new": not existing_user}

