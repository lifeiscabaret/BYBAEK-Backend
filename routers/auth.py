from fastapi import APIRouter, HTTPException, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.logging import logger
import os
import requests

router = APIRouter()

class InstagramLoginRequest(BaseModel):
    code: str

class InstagramLoginResponse(BaseModel):
    user_id: str

@router.post("/instagram", response_model=InstagramLoginResponse, status_code=status.HTTP_201_CREATED)
async def instagram_business_login(req: InstagramLoginRequest) -> Response:

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

    logger.info(os.getenv("client_id"))
    logger.info(response)
    
    # access token request error
    if not response:
        raise HTTPException(status_code=401, detail="access token not validate")
    
    response = response.json()
    
    # access token response error
    if not response['data']:
        error_type = response['error_type']
        error_code = response['code']
        error_message = response['error_message']
        
        logger.error(f'Access Token Response Error {error_code} {error_type}: {error_message}')
        raise HTTPException(status_code=error_code, datail=error_type)
    
    # get user_id and short_access_token
    user_id = response['data'][0]['user_id']
    short_access_token = response['data'][0]['access_token']
    
    
    # get long-lived access token
    params = {
        'grant_type': 'ig_exchange_token',
        'client_secret': os.getenv("client_secret"),
        'access_token': short_access_token
    }
    response = requests.get("https://graph.instagram.com/access_token", params=params)
    
    # access token response error
    if not response['access_token']:
        error_type = response['error_type']
        error_code = response['code']
        error_message = response['error_message']
        
        logger.error(f'Access Token Response Error {error_code} {error_type}: {error_message}')
        raise HTTPException(status_code=error_code, datail=error_type)
    
    access_token = response['access_token']
    expires_in = response['expires_in']

    return { user_id: user_id }