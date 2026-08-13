import os
import re
import base64
import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import quote

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send']

# 파일 경로: 실행 위치 무관하게 리포지토리 루트 기준으로 찾음.
# [FIX] 예전엔 이 파일 기준(services/)이라 항상 못 찾았다 — token.json/credentials.json 은
# 리포 루트에 있다. 그 결과 매번 InstalledAppFlow 로 떨어졌고, _send_email_sync 의
# try/except 가 이를 삼켜서 초안 알림 메일이 조용히 실패하고 있었다.
_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH") or os.path.join(_BASE_DIR, 'token.json')
_CREDS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH") or os.path.join(_BASE_DIR, 'credentials.json')


def _get_gmail_service():
    """Gmail API 서비스 객체 반환 (토큰 자동 갱신 포함)"""
    creds = None

    if os.path.exists(_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # run_local_server() 는 브라우저 동의를 기다리며 스레드를 무한정 붙잡는다.
            # 서버 프로세스(asyncio.to_thread)에서 그러면 스레드가 영구히 샌다 →
            # 기본은 즉시 실패, 토큰 최초 발급 때만 환경변수로 명시적으로 연다.
            if os.getenv("GMAIL_ALLOW_INTERACTIVE_AUTH") != "1":
                raise RuntimeError(
                    f"Gmail 토큰이 없거나 갱신 불가합니다 ({_TOKEN_PATH}). "
                    "토큰을 새로 발급하려면 GMAIL_ALLOW_INTERACTIVE_AUTH=1 로 실행하세요."
                )
            flow = InstalledAppFlow.from_client_secrets_file(_CREDS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(_TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


def _send_email_sync(to_email: str, subject: str, body: str) -> bool:
    """동기 발송 함수 (asyncio.to_thread로 호출)"""
    try:
        service = _get_gmail_service()

        message = MIMEMultipart("alternative")
        message['to']      = to_email
        message['subject'] = subject
        message.attach(MIMEText(body, 'plain', 'utf-8'))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={'raw': raw}).execute()

        logger.info(f"[email_service] 메일 발송 성공 → {to_email}")
        return True

    except Exception as e:
        logger.error(f"[email_service] 메일 발송 실패: {e}")
        return False


async def send_email(to_email: str, subject: str, body: str) -> bool:
    """
    비동기 래퍼 - FastAPI 컨텍스트에서 블로킹 없이 호출 가능
    """
    return await asyncio.to_thread(_send_email_sync, to_email, subject, body)


# 발송 가능한 주소인지 최소 확인용. 엄격한 RFC 검증이 아니라
# "UPN 이 메일 주소 꼴인가"를 거르는 용도다 (아래 resolve_owner_email 참고).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value) -> bool:
    return bool(value and _EMAIL_RE.match(str(value).strip()))


def resolve_owner_email(shop_auth: dict) -> str:
    """알림을 받을 사장님 주소를 정한다. 못 구하면 None.

    [왜 이 함수가 따로 있나]
    같은 폴백 체인이 auth 라우터(마이페이지 표시용)와 알림 발송 쪽에 따로 적혀 있었고,
    알림 쪽만 name 이 빠져 있었다. 그 결과 라이브 샵 3곳 모두 owner_email 이 비어
    "알림 메일이 한 번도 나가지 않는" 상태였다. 체인을 한 곳에 모아 다시 갈라지지 않게 한다.

    우선순위: owner_email(사장님이 직접 설정) > gmail(레거시) > name(MS 로그인 principal).
    name 은 AAD UPN 이라 항상 수신 가능한 메일함은 아니다(예: user@tenant.onmicrosoft.com)
    → 이메일 형식일 때만 쓴다.
    """
    if not shop_auth:
        return None

    for key in ("owner_email", "gmail", "name"):
        value = shop_auth.get(key)
        if looks_like_email(value):
            if key != "owner_email":
                logger.info(f"[email_service] owner_email 미설정 → '{key}' 값으로 폴백")
            return str(value).strip()

    return None


def _frontend_base_url() -> str:
    """검토 링크에 쓸 프론트엔드 도메인.

    FRONTEND_URL은 CORS용으로 콤마 구분 목록일 수 있어(main.py 참고) 첫 항목만 쓴다.
    """
    raw = os.getenv("FRONTEND_URL", "http://localhost:3000")
    first = next((o.strip() for o in raw.split(",") if o.strip()), "http://localhost:3000")
    return first.rstrip("/")


def build_review_url(shop_id: str, post_id: str) -> str:
    """/review 페이지가 초안을 불러오려면 shop_id와 post_id가 둘 다 필요하다.

    거기에 검토 전용 서명 토큰(t)을 더 싣는다. 프론트는 세션 토큰을 sessionStorage에
    두는데 sessionStorage는 탭 단위라, 메일 앱에서 열린 새 탭에는 세션이 없다.
    t가 없으면 이 링크는 사실상 항상 401이 난다.
    t는 이 post_id 하나에만 통하는 스코프 제한 토큰이다(issue_review_token 참고).
    """
    url = (
        f"{_frontend_base_url()}/review"
        f"?shop_id={quote(str(shop_id), safe='')}&post_id={quote(str(post_id), safe='')}"
    )
    try:
        from auth.token_verify import issue_review_token
        url += f"&t={quote(issue_review_token(str(shop_id), str(post_id)), safe='')}"
    except Exception as e:
        # JWT_SECRET 미설정 등. 링크 자체는 살려둔다 — 앱에 로그인된 상태면 그대로 동작한다.
        logger.warning(f"[email_service] 검토 토큰 생성 실패 → 토큰 없는 링크로 발송: {e}")
    return url


async def send_draft_notification(
    to_email: str, post_id: str, caption: str, shop_id: str = None
) -> bool:
    """초안 완성 알림.

    [FIX] 예전엔 본문에 post_id 문자열만 있고 클릭할 링크가 없어서, 사장님이 초안을
    열어볼 방법이 사실상 없었다. shop_id까지 담은 /review 링크를 본문에 넣는다.
    (shop_id 없이는 /review가 초안을 조회하지 못한다.)
    """
    subject = "[ByBaek] 새 게시물 초안이 준비됐어요 ✂️"
    preview = f"{caption[:100]}{'...' if len(caption) > 100 else ''}"

    if shop_id:
        action_block = (
            f"👉 아래 링크에서 확인하고 수정/승인해주세요. (48시간 동안 유효)\n"
            f"{build_review_url(shop_id, post_id)}\n"
        )
    else:
        # shop_id를 못 구한 예외 상황: 링크를 만들 수 없으므로 식별자만 남긴다.
        logger.warning(f"[email_service] shop_id 없이 초안 알림 발송 → 링크 생략 (post_id={post_id})")
        action_block = "👉 앱에서 확인 후 승인 / 수정 / 취소를 선택해주세요.\n"

    body = (
        f"안녕하세요! 오늘의 인스타 게시물 초안이 완성됐어요.\n\n"
        f"📝 초안 미리보기:\n{preview}\n\n"
        f"{action_block}\n"
        f"[ post_id: {post_id} ]"
    )
    return await send_email(to_email, subject, body)