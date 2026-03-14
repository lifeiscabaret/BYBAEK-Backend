import os
import base64
import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send']

# 파일 경로: 실행 위치 무관하게 이 파일 기준으로 찾음
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
_TOKEN_PATH = os.path.join(_BASE_DIR, 'token.json')
_CREDS_PATH = os.path.join(_BASE_DIR, 'credentials.json')


def _get_gmail_service():
    """Gmail API 서비스 객체 반환 (토큰 자동 갱신 포함)"""
    creds = None

    if os.path.exists(_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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


async def send_draft_notification(to_email: str, post_id: str, caption: str) -> bool:
    subject = "[ByBaek] 새 게시물 초안이 준비됐어요 ✂️"
    body = (
        f"안녕하세요! 오늘의 인스타 게시물 초안이 완성됐어요.\n\n"
        f"📝 초안 미리보기:\n{caption[:100]}{'...' if len(caption) > 100 else ''}\n\n"
        f"앱에서 확인 후 OK / 수정 / 취소를 선택해주세요.\n\n"
        f"[ post_id: {post_id} ]"
    )
    return await send_email(to_email, subject, body)