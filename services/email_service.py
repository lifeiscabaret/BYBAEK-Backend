import os
import json
import hmac
import hashlib
import base64
import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def generate_email_token(shop_id: str, post_id: str) -> str:
    """이메일 액션 버튼용 HMAC 토큰 생성 (32자 hex)"""
    secret = os.getenv("EMAIL_ACTION_SECRET", "bybaek-default-secret-2026")
    message = f"{shop_id}:{post_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()[:32]


def verify_email_token(shop_id: str, post_id: str, token: str) -> bool:
    """이메일 액션 토큰 검증"""
    expected = generate_email_token(shop_id, post_id)
    return hmac.compare_digest(expected, token)


def _get_gmail_service():
    """
    Gmail API 서비스 객체 반환.

    환경변수 GMAIL_TOKEN_JSON (JSON 문자열) 에서 토큰을 로드합니다.
    access_token이 만료된 경우 refresh_token으로 자동 갱신합니다.
    로컬 파일(token.json)은 사용하지 않습니다.
    """
    token_json_str = os.getenv("GMAIL_TOKEN_JSON")
    if not token_json_str:
        raise RuntimeError(
            "환경변수 GMAIL_TOKEN_JSON이 설정되지 않았습니다. "
            "로컬에서 token.json을 생성한 뒤 해당 내용을 환경변수에 등록하세요."
        )

    token_data = json.loads(token_json_str)
    creds = Credentials.from_authorized_user_info(token_data, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            logger.info("[email_service] access_token 갱신 완료")
        else:
            raise RuntimeError(
                "Gmail 토큰이 만료되었고 refresh_token도 유효하지 않습니다. "
                "GMAIL_TOKEN_JSON 환경변수를 새로 발급한 토큰으로 교체하세요."
            )

    return build('gmail', 'v1', credentials=creds)


def _send_email_sync(to_email: str, subject: str, html_body: str) -> bool:
    """동기 발송 함수 (asyncio.to_thread로 호출) — HTML 이메일"""
    try:
        service = _get_gmail_service()

        message = MIMEMultipart("alternative")
        message['to']      = to_email
        message['subject'] = subject
        message.attach(MIMEText(html_body, 'html', 'utf-8'))

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={'raw': raw}).execute()

        logger.info(f"[email_service] 메일 발송 성공 → {to_email}")
        return True

    except Exception as e:
        logger.error(f"[email_service] 메일 발송 실패: {e}")
        return False


async def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """비동기 래퍼 - FastAPI 컨텍스트에서 블로킹 없이 호출 가능"""
    return await asyncio.to_thread(_send_email_sync, to_email, subject, html_body)


def _build_draft_email_html(caption: str, approve_url: str, edit_url: str, reject_url: str) -> str:
    """승인/수정/거절 버튼이 포함된 HTML 이메일 본문 생성"""
    caption_preview = caption[:80] + ("..." if len(caption) > 80 else "")
    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:40px auto;background:#ffffff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">

    <div style="background:#1a1a2e;padding:24px 32px;">
      <h1 style="margin:0;color:#ffffff;font-size:20px;letter-spacing:2px;">BYBAEK</h1>
      <p style="margin:4px 0 0;color:#aaa;font-size:13px;">바버샵 AI 마케팅 자동화</p>
    </div>

    <div style="padding:32px;">
      <h2 style="margin:0 0 8px;color:#222;font-size:18px;">새 인스타그램 게시물 검토 요청</h2>
      <p style="margin:0 0 24px;color:#666;font-size:14px;">AI가 새 게시물 초안을 준비했습니다. 아래 버튼으로 바로 처리해 주세요.</p>

      <div style="background:#f8f9fa;border-left:4px solid #1a1a2e;border-radius:4px;padding:16px;margin-bottom:28px;">
        <p style="margin:0 0 4px;font-size:12px;color:#999;font-weight:bold;text-transform:uppercase;">게시물 미리보기</p>
        <p style="margin:0;color:#333;font-size:15px;line-height:1.6;">{caption_preview}</p>
      </div>

      <table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:24px;">
        <tr>
          <td align="center" style="padding:6px;">
            <a href="{approve_url}"
               style="display:inline-block;background:#28a745;color:#ffffff;text-decoration:none;padding:14px 28px;border-radius:6px;font-size:15px;font-weight:bold;min-width:120px;text-align:center;">
              ✅ 승인
            </a>
          </td>
          <td align="center" style="padding:6px;">
            <a href="{edit_url}"
               style="display:inline-block;background:#0066cc;color:#ffffff;text-decoration:none;padding:14px 28px;border-radius:6px;font-size:15px;font-weight:bold;min-width:120px;text-align:center;">
              ✏️ 수정
            </a>
          </td>
          <td align="center" style="padding:6px;">
            <a href="{reject_url}"
               style="display:inline-block;background:#dc3545;color:#ffffff;text-decoration:none;padding:14px 28px;border-radius:6px;font-size:15px;font-weight:bold;min-width:120px;text-align:center;">
              ❌ 거절
            </a>
          </td>
        </tr>
      </table>

      <p style="margin:0;color:#999;font-size:12px;text-align:center;">
        · 승인: 인스타그램에 바로 업로드됩니다<br>
        · 수정: 프론트 검토 페이지에서 캡션을 수정할 수 있습니다<br>
        · 거절: 해당 게시물 자동 업로드가 취소됩니다
      </p>
    </div>

    <div style="background:#f8f8f8;padding:16px 32px;border-top:1px solid #eee;">
      <p style="margin:0;color:#bbb;font-size:11px;text-align:center;">
        © BYBAEK · 이 메일은 자동 발송된 알림입니다
      </p>
    </div>
  </div>
</body>
</html>"""


async def send_draft_notification(to_email: str, post_id: str, caption: str, shop_id: str = "") -> bool:
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    backend_url  = os.getenv("BACKEND_URL", "https://bybaek-b-bzhhgzh8d2gthpb3.koreacentral-01.azurewebsites.net")

    token = generate_email_token(shop_id, post_id)

    approve_url = f"{backend_url}/api/agent/email-action?action=approve&shop_id={shop_id}&post_id={post_id}&token={token}"
    reject_url  = f"{backend_url}/api/agent/email-action?action=reject&shop_id={shop_id}&post_id={post_id}&token={token}"
    edit_url    = f"{frontend_url}/review?shop_id={shop_id}&post_id={post_id}"

    subject   = "[BYBAEK] 새 인스타그램 게시물 검토 요청"
    html_body = _build_draft_email_html(caption, approve_url, edit_url, reject_url)
    return await send_email(to_email, subject, html_body)
