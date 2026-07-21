from fastapi import APIRouter, HTTPException, Request, Response, status, Depends
from starlette.background import BackgroundTask
from pydantic import BaseModel
from utils.logging import logger
import os
import json
import html
import requests
from services.cosmos_db import save_auth, get_auth
from auth.token_verify import issue_token, get_current_shop, require_shop_owner
from agents.insta_analyzer import analyze_instagram_history
import logging
from datetime import datetime
from fastapi.responses import RedirectResponse, HTMLResponse

router = APIRouter()

class InstagramLoginRequest(BaseModel):
    code: str


def _establish_identity(request: Request) -> dict:
    """Easy Auth 헤더로 신원을 확인하고, Shop 문서를 갱신한 뒤 자체 발급 토큰을 반환.

    Easy Auth 가 주입하는 헤더(X-MS-CLIENT-PRINCIPAL-ID 등)는 팝업이 착지한
    same-origin(api2...) 컨텍스트에서만 신뢰 가능하다. 헤더가 없으면 401.
    반환: {"shop_id", "is_new", "access_token"}
    """
    ms_user_id   = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    ms_user_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")

    # Easy Auth 로 신원 확인에 실패하면 여기서 끝. 기본 계정 폴백 없음.
    if not ms_user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    current_time = datetime.utcnow().isoformat()
    existing_user = get_auth(ms_user_id)

    auth_data = {
        "name":            ms_user_name,
        "last_login_at":   current_time,
        "is_ms_connected": True,
    }

    # ── refresh_token 저장 ──
    # Worker가 OneDrive 파일 다운로드 시 토큰 만료 문제 해결용.
    # Easy Auth 설정에서 offline_access 스코프 필요.
    refresh_token = request.headers.get("x-ms-token-aad-refresh-token")
    if refresh_token:
        auth_data["refresh_token"] = refresh_token

    if not existing_user:
        auth_data["created_at"] = current_time
        logging.info(f"신규 유저 가입: {ms_user_id}")
    else:
        logging.info(f"기존 유저 로그인: {ms_user_id}")

    save_auth(ms_user_id, auth_data)

    # 신원 확인 성공 → 이후 API 호출용 백엔드 자체 발급 토큰 발행.
    access_token = issue_token(ms_user_id)

    return {
        "shop_id": ms_user_id,
        "is_new": not existing_user,
        "access_token": access_token,
    }


@router.get("/ms/callback")
async def ms_callback(request: Request):
    """로그인 팝업이 Easy Auth 인증을 마치고 착지하는 same-origin(api2...) 페이지.

    여기서 서버사이드로 Easy Auth 쿠키/헤더를 읽어 토큰을 발급하고,
    opener(실제 앱 탭)에 postMessage 로 전달한 뒤 팝업을 닫는다.
    opener 가 별도로 /auth/me 를 크로스오리진으로 호출할 필요가 없어져
    로그인 전 과정에서 크로스오리진 쿠키를 읽어야 하는 지점이 사라진다.
    """
    target_origin = os.getenv("FRONTEND_URL", "http://localhost:3000")
    try:
        identity = _establish_identity(request)
        payload = {
            "type": "MS_LOGIN_SUCCESS",
            "shop_id": identity["shop_id"],
            "is_new": identity["is_new"],
            "access_token": identity["access_token"],
        }
        status_msg = "로그인 성공. 창을 닫는 중…"
    except HTTPException as e:
        payload = {"type": "MS_LOGIN_ERROR", "detail": str(e.detail)}
        status_msg = "로그인에 실패했습니다. 이 창을 닫고 다시 시도해주세요."

    # 값은 JSON 으로 안전하게 직렬화해 스크립트 컨텍스트 이탈을 방지.
    payload_json = json.dumps(payload)
    origin_json = json.dumps(target_origin)
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>로그인 처리 중</title></head><body>
<p>{html.escape(status_msg)}</p>
<script>
  (function() {{
    var payload = {payload_json};
    var targetOrigin = {origin_json};
    try {{
      if (window.opener) {{
        window.opener.postMessage(payload, targetOrigin);
      }}
    }} catch (e) {{}}
    setTimeout(function() {{ try {{ window.close(); }} catch (e) {{}} }}, 300);
  }})();
</script>
</body></html>"""
    return HTMLResponse(content=page)

@router.get("/instagram")
async def instagram_business_login(code: str, res: Response, fast_req: Request):

    access_token = fast_req.headers.get("x-ms-token-aad-access-token")
    logger.info(f"access token = {access_token}")

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

    ms_id = fast_req.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    if not ms_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    insta_data = {
        "insta_access_token": access_token,
        "insta_user_id":      user_id,
        "insta_updated_at":   datetime.utcnow().isoformat()
    }
    save_auth(ms_id, insta_data)

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    return RedirectResponse(
        url=f"{frontend_url}/auth/callback?type=instagram",
        background=BackgroundTask(analyze_instagram_history, ms_id)
    )


@router.get("/status/{shop_id}")
async def get_auth_status(shop_id: str, current_shop: dict = Depends(get_current_shop)):
    require_shop_owner(current_shop, shop_id)
    auth = get_auth(shop_id)
    if not auth:
        raise HTTPException(status_code=404, detail="샵 정보를 찾을 수 없습니다.")

    is_onedrive_connected = bool(
        auth.get("one_delta_link") or auth.get("onedrive_token")
    )
    is_insta_connected = bool(auth.get("insta_access_token"))

    # 마이페이지 표시용 인스타 계정명 (연동 시 insta_analyzer가 저장해 둔 값을 읽기만 함).
    # 미저장/미연동이면 null 폴백 — 여기서 Graph 호출은 하지 않음(빠르고 rate-limit 안전).
    return {
        "is_onedrive_connected": is_onedrive_connected,
        "is_insta_connected": is_insta_connected,
        "instagram_username": auth.get("insta_username") if is_insta_connected else None,
        # 마이페이지 표시용 MS 로그인 이메일. owner_email 우선, 없으면 /auth/me가 저장한 로그인 principal(name).
        # shop_id는 검증된 토큰에서 확정되며(require_shop_owner), Graph 호출 없음.
        "email": auth.get("owner_email") or auth.get("name"),
    }


@router.get("/me")
async def get_my_info(request: Request):
    """직접 호출용(팝업 postMessage 경로가 아닌 경우) 신원 확인 + 토큰 발급 JSON.

    주의: opener 창이 이걸 크로스오리진 AJAX 로 호출하면 Easy Auth 쿠키가
    실리지 않아 401 이 난다. 로그인 직후 토큰 획득은 /ms/callback 의 postMessage
    경로를 사용할 것. 이 엔드포인트는 same-origin 컨텍스트에서만 정상 동작한다.
    """
    return _establish_identity(request)