"""이메일 주소 관련 순수 유틸 — 외부 의존성 없음.

[왜 services/email_service.py 에서 분리했나]
email_service 는 최상단에서 google-auth / google-api-python-client 를 import 한다.
로그인 경로(_persist_and_issue)가 owner_email 자동 채움을 위해 거기서
looks_like_email 을 가져다 쓰는 순간, 로그인이 Gmail 발송용 무거운 의존성에
묶여버렸다. 프로덕션 requirements.txt 에 그 패키지들이 빠져 있어
ModuleNotFoundError → MS 로그인 전체가 500 으로 죽었다.

메일을 '보내는' 기능과 주소를 '판별하는' 기능은 의존성이 다르다.
판별 쪽은 여기(표준 라이브러리만)에 두고, 로그인 같은 무관한 경로가
발송 의존성에 발목 잡히지 않게 한다.
"""

import re

# 발송 가능한 주소인지 최소 확인용. 엄격한 RFC 검증이 아니라
# "AAD UPN 이 메일 주소 꼴인가"를 거르는 용도다.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def looks_like_email(value) -> bool:
    return bool(value and _EMAIL_RE.match(str(value).strip()))


def mask_email(value) -> str:
    """로그용 이메일 마스킹. PII 를 로그에 평문으로 남기지 않기 위함.

    예) "hyunji.lee@gmail.com" -> "h********e@gmail.com"
        "ab@x.com"            -> "**@x.com"   (로컬파트 2자 이하는 전체 가림)
    이메일 형식이 아니면 "<no-email>" 로 대체(원문 노출 금지).
    """
    if not looks_like_email(value):
        return "<no-email>"
    local, _, domain = str(value).strip().partition("@")
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}"
    return f"{masked_local}@{domain}"
