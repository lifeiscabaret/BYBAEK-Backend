"""
기능   : Azure AI Foundry의 Anthropic(Claude) 엔드포인트용 AAD(Entra) 인증 헬퍼
배경   :
    - bybaek-foundry의 /anthropic 경로는 api-key 인증을 받지 않고 AAD Bearer 토큰만 허용한다.
      (api-key 방식은 401 → post_writer 등에서 fallback(목업) 캡션이 반환되던 원인)
    - DefaultAzureCredential이 실행 환경에 맞는 자격증명을 자동 선택:
        · 로컬        : az login 사용자 자격증명
        · App Service : 시스템 할당 Managed Identity
사용   :
    from utils.claude_auth import CLAUDE_BASE_URL, get_claude_token
    anthropic.AsyncAnthropic(base_url=CLAUDE_BASE_URL, auth_token=get_claude_token())
"""

import os

from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# base_url 뒤에 anthropic SDK가 /v1/messages 를 붙인다 → 경로는 .../anthropic 까지만.
CLAUDE_BASE_URL = os.getenv(
    "AZURE_CLAUDE_ENDPOINT",
    "https://bybaek-foundry.services.ai.azure.com/anthropic",
)

# Cognitive Services 데이터 플레인 스코프
_SCOPE = "https://cognitiveservices.azure.com/.default"

# 단일 크리덴셜 인스턴스 → 토큰 캐시/자동 갱신 공유 (모듈 로드 시 네트워크 호출 없음)
#
# [중요] exclude_environment_credential=True
#   이 앱엔 OneDrive/Graph용 AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET 가 환경변수로 설정돼 있어,
#   기본 DefaultAzureCredential은 Managed Identity보다 먼저 그 SP(EnvironmentCredential)로 토큰을
#   발급한다. 그 SP는 bybaek-foundry에 Cognitive Services 역할이 없어 Claude 호출이 403 → 목업으로
#   빠졌다. 환경 자격증명을 제외하면:
#     · App Service : Managed Identity (Cognitive Services User 역할 보유) 사용 → 정상
#     · 로컬        : az login 자격증명(AzureCliCredential) 사용 → 정상
_credential = DefaultAzureCredential(exclude_environment_credential=True)
_token_provider = get_bearer_token_provider(_credential, _SCOPE)


def get_claude_token() -> str:
    """유효한 AAD Bearer 토큰 반환. 만료가 임박하면 내부적으로 자동 갱신된다."""
    return _token_provider()
