from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.logging import logger
import os
import requests
from auth.appService_auth_check import appService_auth_check
from services.cosmos_db import save_auth, get_auth
import logging
from datetime import datetime

router = APIRouter()

class InstagramLoginRequest(BaseModel):
    code: str

@router.post("/instagram", status_code=status.HTTP_201_CREATED)
async def instagram_business_login(req: InstagramLoginRequest, res: Response, fast_req: Request) -> Response:
    
    # app service auth check
    appService_auth_check(req)

    # instagram authentication redirect parameter
    code = req.code

    # if cannot get code
    if not code:
        raise HTTPException(status_code=401, detail="authorize code doesnt exist")
        
    # get access token
    payload = {
        'client_id': (None, os.getenv("client_id")),
        'client_secret': (None, os.getenv("client_secret")),
        'grant_type': (None, "authorization_code"),
        'redirect_uri': (None, os.getenv("redirect_uri")),
        'code': (None, code)
    }
    
    response = requests.post("https://api.instagram.com/oauth/access_token", files=payload)
    response = response.json()
    
    # access token response error
    if 'error' in response:
        error_type = response['type']
        error_code = response['code']
        error_message = response['message']
        
        logger.error(f'Access Token Response Error {error_code} {error_type}: {error_message}')
        raise HTTPException(status_code=error_code, datail=error_type)
    
    # get user_id and short_access_token
    user_id = response['user_id']
    short_access_token = response['access_token']
    
    
    # get long-lived access token
    params = {
        'grant_type': 'ig_exchange_token',
        'client_secret': os.getenv("client_secret"),
        'access_token': short_access_token
    }
    
    response = requests.get("https://graph.instagram.com/access_token", params=params)
    response = response.json()
    
    # access token response error
    if 'error' in response:
        error_type = response['type']
        error_code = response['code']
        error_message = response['message']
        
        logger.error(f'Access Token Response Error {error_code} {error_type}: {error_message}')
        raise HTTPException(status_code=error_code, datail=error_type)
    
    access_token = response['access_token']
    expires_in = response['expires_in']
    
    res.set_cookie(
        key="user_id",
        value=user_id,
        httponly=True,
        secure=True,
        samesite="none"
    )
    
    logger.info(fast_req.cookies)

    # 현재 로그인된 MS 유저 ID 가져오기
    ms_id = fast_req.headers.get("X-MS-CLIENT-PRINCIPAL-ID") or "test_barber_jiyeon"
    
    # 토큰 정보를 기존 Shop 데이터에 추가
    insta_data = {
        "insta_access_token": access_token,
        "insta_user_id": user_id,
        "insta_updated_at": datetime.utcnow().isoformat()
    }
    save_auth(ms_id, insta_data) # 기존 데이터와 합쳐짐

    return { 'access_token': access_token, "user_id": user_id }

@router.get("/me")
async def get_my_info(request: Request):
    ms_user_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    ms_user_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    
    if not ms_user_id:
        # 로컬에서 uvicorn으로 돌릴 때 헤더가 없으므로 강제로 ID를 할당
        ms_user_id = "test_barber_jiyeon" 
        ms_user_name = "jiyeon@test.com"

    current_time = datetime.utcnow().isoformat()

    # 1. 기존 유저인지 확인 (get_auth 활용)
    existing_user = get_auth(ms_user_id)
    
    auth_data = {
        "name": ms_user_name,
        "last_login_at": current_time, # 매번 업데이트
    }

    if not existing_user:
        # 최초 가입 시점에만 created_at 추가
        auth_data["created_at"] = current_time
        logging.info(f"신규 유저 가입: {ms_user_id}")
    else:
        logging.info(f"기존 유저 로그인: {ms_user_id}")

    # 2. 정보 저장 (기존 정보와 병합됨)
    save_auth(ms_user_id, auth_data)

    return {"shop_id": ms_user_id, "is_new": not existing_user}