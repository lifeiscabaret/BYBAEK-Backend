"""
이메일 발송 로컬 테스트 스크립트
실행: python services/test_email.py
"""
import os
import sys

# token_output.txt에서 토큰 로드 (로컬 테스트용)
token_path = os.path.join(os.path.dirname(__file__), "token_output.txt")
if os.path.exists(token_path):
    with open(token_path, "r") as f:
        os.environ["GMAIL_TOKEN_JSON"] = f.read().strip()
    print(f"[test] token_output.txt 로드 완료")
else:
    print("[test] token_output.txt 없음 → GMAIL_TOKEN_JSON 환경변수 사용")

# 테스트 수신 이메일 (아래 주소를 변경하세요)
TEST_TO_EMAIL = "audrms747@naver.com"
TEST_SHOP_ID  = "test_shop_001"
TEST_POST_ID  = "post_test001"
TEST_CAPTION  = "오늘의 스타일 완성! 깔끔한 페이드컷으로 새로운 분위기를 만들어보세요. #바버샵 #페이드컷"

import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from services.email_service import send_draft_notification

async def main():
    print(f"[test] 발송 시작 → {TEST_TO_EMAIL}")
    result = await send_draft_notification(
        to_email=TEST_TO_EMAIL,
        post_id=TEST_POST_ID,
        caption=TEST_CAPTION,
        shop_id=TEST_SHOP_ID,
    )
    if result:
        print("[test] ✅ 발송 성공!")
    else:
        print("[test] ❌ 발송 실패. 위 로그 확인")

asyncio.run(main())
