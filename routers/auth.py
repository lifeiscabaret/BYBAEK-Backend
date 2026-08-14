from fastapi import APIRouter, HTTPException, Request, Response, status, Depends
from starlette.background import BackgroundTask
from pydantic import BaseModel
from utils.logging import logger
import os
import json
import html
import requests
import msal
from jose import JWTError
from services.cosmos_db import save_auth, get_auth
from auth.token_verify import (
    issue_token, get_current_shop, require_shop_owner,
    sign_short_lived, verify_short_lived,
)
from agents.insta_analyzer import analyze_instagram_history
import logging
from datetime import datetime, timedelta, timezone
from fastapi.responses import RedirectResponse, HTMLResponse

router = APIRouter()

# ── 백엔드 자체 소유 OAuth (Authorization Code) 설정 ──
# Easy Auth 의 OAuth 왕복을 우리가 대체한다. response_mode=query(GET 콜백) +
# first-party SameSite=Lax 쿠키로 flow 상태를 나르므로, Easy Auth 가 form_post
# 콜백에 쓰던 SameSite=None Nonce 쿠키(시크릿창에서 서드파티로 차단)를 피한다.
MS_AUTHORITY = "https://login.microsoftonline.com/common"  # 개인 MS 계정 포함 → 반드시 /common
MS_LOGIN_SCOPES = ["Files.Read"]  # OneDrive 워커 호환 (openid/profile/offline_access 는 MSAL 자동 추가)
FLOW_COOKIE = "ms_auth_flow"
FLOW_TTL_SECONDS = 600  # 로그인 개시~콜백 허용 시간 (10분)


class InstagramLoginRequest(BaseModel):
    code: str


def _build_msal_app() -> msal.ConfidentialClientApplication:
    """OneDrive 워커(_acquire_graph_token_from_refresh)와 동일한 앱/authority 구성.
    그래야 여기서 받은 refresh_token 을 워커가 그대로 갱신에 쓸 수 있다."""
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.error("[auth] AZURE_CLIENT_ID/SECRET 미설정 — OAuth 로그인 불가")
        raise HTTPException(status_code=500, detail="서버 인증 설정 오류")
    return msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=MS_AUTHORITY,
        client_credential=client_secret,
    )


def _oauth_redirect_uri() -> str:
    """App Registration 에 등록된 값과 정확히 일치해야 한다."""
    return os.getenv(
        "OAUTH_REDIRECT_URI",
        "https://api2.bybaekofficial.com/api/auth/callback",
    )


def _frontend_origin() -> str:
    return os.getenv("FRONTEND_URL", "http://localhost:3000")


def _persist_and_issue(ms_user_id: str, ms_user_name: str, refresh_token: str = None) -> dict:
    """신원(shop_id=oid)으로 Shop 문서를 갱신하고 자체 발급 토큰을 반환.
    Easy Auth 헤더 경로와 자체 OAuth 경로가 공유하는 최종 단계."""
    current_time = datetime.utcnow().isoformat()
    existing_user = get_auth(ms_user_id)

    auth_data = {
        "name":            ms_user_name,
        "last_login_at":   current_time,
        "is_ms_connected": True,
    }
    # refresh_token: Worker 가 OneDrive 파일 다운로드용 Graph 토큰 갱신에 사용.
    if refresh_token:
        auth_data["refresh_token"] = refresh_token

    # 알림 수신 주소 자동 채움.
    # 검토 알림 메일을 보낼 owner_email 을 실제로 입력받는 화면이 라이브에 하나도 없어서
    # (OnboardingSurvey 컴포넌트는 어디에서도 렌더되지 않음) 전 샵이 비어 있었고,
    # 그래서 초안 알림이 한 번도 발송되지 않았다. MS 로그인 principal 이 이미
    # 이메일 형식으로 들어오므로 이걸 기본값으로 깔아준다.
    # 사장님이 직접 설정한 값은 절대 덮어쓰지 않는다 — 비어 있을 때만 채운다.
    # utils.email_utils 에서 가져온다 — services.email_service 는 최상단에서
    # google-auth / googleapiclient 를 import 하므로, 그쪽을 참조하면 로그인이
    # Gmail 발송 의존성에 묶인다 (그래서 실제로 로그인이 통째로 500 났다).
    from utils.email_utils import looks_like_email
    if not (existing_user or {}).get("owner_email") and looks_like_email(ms_user_name):
        auth_data["owner_email"] = str(ms_user_name).strip()
        logging.info(f"[auth] owner_email 자동 설정 ({ms_user_id})")

    if not existing_user:
        auth_data["created_at"] = current_time
        logging.info(f"신규 유저 가입: {ms_user_id}")
    else:
        logging.info(f"기존 유저 로그인: {ms_user_id}")

    save_auth(ms_user_id, auth_data)
    access_token = issue_token(ms_user_id)
    return {
        "shop_id": ms_user_id,
        "is_new": not existing_user,
        "access_token": access_token,
    }


def _establish_identity(request: Request) -> dict:
    """Easy Auth 헤더로 신원을 확인하고 자체 발급 토큰을 반환 (레거시/전환기 경로).

    Easy Auth 가 주입하는 헤더(X-MS-CLIENT-PRINCIPAL-ID 등)는 same-origin(api2...)
    컨텍스트에서만 신뢰 가능. 헤더가 없으면 401 — 기본 계정 폴백 없음.
    """
    ms_user_id   = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    ms_user_name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if not ms_user_id:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    refresh_token = request.headers.get("x-ms-token-aad-refresh-token")
    return _persist_and_issue(ms_user_id, ms_user_name, refresh_token)


def _popup_message_html(payload: dict, target_origin: str, status_msg: str) -> str:
    """opener 로 postMessage 후 자동으로 닫히는 팝업 페이지.
    값은 JSON 직렬화 + 메시지는 html escape 로 스크립트 이탈을 방지."""
    payload_json = json.dumps(payload)
    origin_json = json.dumps(target_origin)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
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


@router.get("/login")
async def ms_login():
    """자체 OAuth 로그인 개시. 팝업이 이 경로를 열면 Microsoft 인증 페이지로 리다이렉트.

    Easy Auth 의 /.auth/login/aad 를 대체한다. flow 상태(state/nonce/PKCE)는
    first-party SameSite=Lax; Secure; HttpOnly 쿠키에 서명해 담는다 —
    콜백이 top-level GET(response_mode=query)이라 Lax 쿠키가 정상 전송되고,
    시크릿창에서도 first-party 라 차단되지 않는다.
    """
    msal_app = _build_msal_app()
    flow = msal_app.initiate_auth_code_flow(
        scopes=MS_LOGIN_SCOPES,
        redirect_uri=_oauth_redirect_uri(),
        response_mode="query",
    )
    auth_uri = flow.get("auth_uri")
    if not auth_uri:
        raise HTTPException(status_code=500, detail="로그인 개시 실패")

    # 쿠키에는 auth_uri(대용량) 제외한 flow 만 서명해 저장.
    flow_to_store = {k: v for k, v in flow.items() if k != "auth_uri"}
    flow_token = sign_short_lived(flow_to_store, FLOW_TTL_SECONDS)

    resp = RedirectResponse(url=auth_uri)
    resp.set_cookie(
        key=FLOW_COOKIE,
        value=flow_token,
        max_age=FLOW_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/api/auth",
    )
    return resp


@router.get("/callback")
async def ms_login_callback(request: Request):
    """Microsoft 인증 후 착지하는 same-origin(api2...) 콜백.

    쿠키의 flow 와 쿼리스트링(code/state)을 MSAL 로 교환해 신원을 확인하고,
    자체 발급 토큰을 opener 로 postMessage 한 뒤 팝업을 닫는다.
    """
    target_origin = _frontend_origin()
    try:
        flow_token = request.cookies.get(FLOW_COOKIE)
        if not flow_token:
            raise HTTPException(status_code=400, detail="로그인 세션이 만료되었거나 쿠키가 차단되었습니다")
        try:
            flow = verify_short_lived(flow_token)
        except JWTError:
            raise HTTPException(status_code=400, detail="로그인 세션이 유효하지 않습니다")

        msal_app = _build_msal_app()
        result = msal_app.acquire_token_by_auth_code_flow(flow, dict(request.query_params))
        if "error" in result:
            logger.error(f"[auth] 토큰 교환 실패: {result.get('error')} / {result.get('error_description')}")
            raise HTTPException(status_code=400, detail="Microsoft 인증에 실패했습니다")

        claims = result.get("id_token_claims", {}) or {}
        # shop_id 는 Easy Auth 의 X-MS-CLIENT-PRINCIPAL-ID 와 동일한 AAD oid.
        ms_user_id = claims.get("oid") or claims.get("sub")
        if not ms_user_id:
            raise HTTPException(status_code=400, detail="사용자 식별자를 확인할 수 없습니다")
        ms_user_name = claims.get("preferred_username") or claims.get("email") or claims.get("name")

        identity = _persist_and_issue(ms_user_id, ms_user_name, result.get("refresh_token"))
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

    resp = HTMLResponse(content=_popup_message_html(payload, target_origin, status_msg))
    resp.delete_cookie(FLOW_COOKIE, path="/api/auth")
    return resp


@router.get("/ms/callback")
async def ms_callback(request: Request):
    """[레거시] Easy Auth 로그인 팝업 착지 경로. Easy Auth 헤더가 있으면 토큰을 발급해
    postMessage 한다. 자체 OAuth 로 전환하면(/login, /callback) 이 경로는 불필요해진다."""
    target_origin = _frontend_origin()
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
    return HTMLResponse(content=_popup_message_html(payload, target_origin, status_msg))


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

    # 장기 토큰은 60일 만료 → 만료 절대 시각을 함께 저장한다.
    # workers/insta_token_refresh.py 가 이 값을 보고 만료 전에 미리 갱신한다.
    issued_at = datetime.now(timezone.utc)
    insta_data = {
        "insta_access_token":          access_token,
        "insta_user_id":               user_id,
        "insta_expires_in":            expires_in,
        "insta_token_expires_at":      (issued_at + timedelta(seconds=int(expires_in))).isoformat(),
        "insta_updated_at":            issued_at.isoformat(),
        "insta_token_needs_reconnect": False,
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