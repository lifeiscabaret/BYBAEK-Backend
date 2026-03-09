from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.logging import logger
import os
import requests
from auth.appService_auth_check import appService_auth_check

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

    return { 'access_token': access_token, "user_id": user_id }