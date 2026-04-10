from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from utils.logging import logger
import os
import requests
from services.cosmos_db import save_auth, get_auth
import logging
from datetime import datetime
from fastapi.responses import RedirectResponse, HTMLResponse

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


# ── Gmail OAuth ──────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


@router.get("/gmail")
async def gmail_oauth_start(shop_id: str):
    """
    Gmail OAuth 시작 — Google 로그인 페이지로 리다이렉트.
    프론트엔드는 이 URL을 팝업으로 열면 됩니다:
        window.open(`/api/auth/gmail?shop_id=${shopId}`)
    """
    client_id = os.getenv("GMAIL_CLIENT_ID")
    redirect_uri = os.getenv("GMAIL_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=500, detail="Gmail OAuth 환경변수(GMAIL_CLIENT_ID, GMAIL_REDIRECT_URI) 미설정")

    try:
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": client_id,
                "client_secret": os.getenv("GMAIL_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            state=shop_id,
            prompt="consent",
            include_granted_scopes="true",
        )
        return RedirectResponse(url=auth_url)
    except Exception as e:
        logging.error(f"Gmail OAuth 시작 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gmail/callback")
async def gmail_oauth_callback(code: str, state: str):
    """
    Gmail OAuth 콜백 — Google로부터 code 수신 후:
    1. access_token + refresh_token 교환
    2. 사용자 이메일 조회
    3. CosmosDB Shop 컨테이너에 토큰 + owner_email + is_gmail_connected 저장
    4. 팝업 창에 GMAIL_LOGIN_SUCCESS 메시지 전송 후 창 닫기
    """
    shop_id = state
    redirect_uri = os.getenv("GMAIL_REDIRECT_URI")
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

    try:
        from google_auth_oauthlib.flow import Flow
        from services.cosmos_db import save_gmail_token

        client_config = {
            "web": {
                "client_id": os.getenv("GMAIL_CLIENT_ID"),
                "client_secret": os.getenv("GMAIL_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }
        flow = Flow.from_client_config(client_config, scopes=GMAIL_SCOPES, redirect_uri=redirect_uri)
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Google userinfo로 이메일 조회
        userinfo_resp = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {credentials.token}"},
            timeout=10,
        )
        userinfo_resp.raise_for_status()
        email = userinfo_resp.json().get("email", "")

        # CosmosDB 저장
        save_gmail_token(
            shop_id=shop_id,
            email=email,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token or "",
        )
        logging.info(f"Gmail 연동 완료 → shop_id={shop_id}, email={email}")

    except Exception as e:
        logging.error(f"Gmail OAuth 콜백 실패: {e}")
        # 실패해도 팝업은 닫되, 실패 메시지 전송
        return HTMLResponse(content=f"""
<html><body><script>
  if (window.opener) {{
    window.opener.postMessage('GMAIL_LOGIN_FAIL', '{frontend_url}');
    window.close();
  }}
</script><p>Gmail 연동 실패: {e}</p></body></html>
""")

    return HTMLResponse(content=f"""
<html><body><script>
  if (window.opener) {{
    window.opener.postMessage('GMAIL_LOGIN_SUCCESS', '{frontend_url}');
    window.close();
  }}
</script><p>Gmail 연동 완료. 이 창을 닫아주세요.</p></body></html>
""")