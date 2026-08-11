"""
기능   : Azure AI Foundry의 Anthropic(Claude) 엔드포인트용 AAD(Entra) 인증 헬퍼
배경   :
    - bybaek-foundry의 /anthropic 경로는 api-key 인증을 받지 않고 AAD Bearer 토큰만 허용한다.
      (api-key 방식은 401 → post_writer 등에서 fallback(목업) 캡션이 반환되던 원인)
    - 실행 환경에 맞는 자격증명을 명시적 체인으로 선택:
        · App Service : 시스템 할당 Managed Identity (ManagedIdentityCredential)
        · 로컬        : az login 자격증명 (AzureCliCredential)
사용   :
    from utils.claude_auth import CLAUDE_BASE_URL, get_claude_token
    anthropic.AsyncAnthropic(base_url=CLAUDE_BASE_URL, auth_token=get_claude_token())
"""

import os

from azure.identity import (
    AzureCliCredential,
    ChainedTokenCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)

# base_url 뒤에 anthropic SDK가 /v1/messages 를 붙인다 → 경로는 .../anthropic 까지만.
CLAUDE_BASE_URL = os.getenv(
    "AZURE_CLAUDE_ENDPOINT",
    "https://bybaek-foundry.services.ai.azure.com/anthropic",
)

# Azure AI Foundry 데이터 플레인 스코프.
# [주의] 이 /anthropic 엔드포인트는 Managed Identity 토큰에 대해 audience로 정확히
#   https://ai.azure.com 를 요구한다. cognitiveservices.azure.com 스코프를 쓰면 MI 토큰이
#   401 "audience is incorrect (https://ai.azure.com)" 로 거부되어 목업(fallback)으로 빠진다.
#   (로컬 az login 사용자 토큰은 두 audience 모두 허용되지만, 배포본 MI는 ai.azure.com 만 통과.)
_SCOPE = "https://ai.azure.com/.default"

# 캡션 생성/인스타 분석에 쓰는 Claude 모델 식별자.
# 예전엔 각 호출부에 "claude-sonnet-4-6"이 하드코딩돼 있어 모델을 바꾸려면 코드를 고쳐야 했다.
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"


def get_claude_model() -> str:
    """CLAUDE_MODEL_NAME 환경변수로 모델을 바꾼다. 없거나 비어 있으면 기존 모델 그대로.

    [주의] 모듈 로드 시점의 os.getenv 상수로 두면 안 된다.
      main.py는 routers를 import한 뒤(10행)에야 load_dotenv()를 호출하므로(14행),
      이 모듈은 .env가 로드되기 전에 import된다. 상수로 굳히면 .env에 적은 값이
      조용히 무시되고 기본값으로 돌아간다(App Service의 실제 환경변수는 동작하지만
      로컬 .env 테스트가 안 됨). 그래서 호출 시점에 읽는다.

    [주의] 빈 문자열도 기본값으로 취급한다.
      .env.example을 그대로 복사하면 'CLAUDE_MODEL_NAME=' 가 되고, dotenv는 이걸
      None이 아니라 ''로 넣는다. 그대로 넘기면 모델명 없이 API를 호출해 실패한다.
    """
    return os.getenv("CLAUDE_MODEL_NAME", "").strip() or DEFAULT_CLAUDE_MODEL

# 단일 크리덴셜 인스턴스 → 토큰 캐시/자동 갱신 공유 (모듈 로드 시 네트워크 호출 없음)
#
# [중요] DefaultAzureCredential 대신 명시적 체인을 쓰는 이유
#   이 앱엔 OneDrive/Graph용 AZURE_CLIENT_ID/TENANT_ID/CLIENT_SECRET 가 환경변수로 설정돼 있다.
#   DefaultAzureCredential은
#     (1) 그 SP(EnvironmentCredential)를 Managed Identity보다 먼저 쓰고,
#     (2) AZURE_CLIENT_ID 를 "user-assigned MI의 client_id"로도 오해해서
#         system-assigned MI 를 못 찾고 실패한다.
#         (실측: "No User Assigned Managed Identity found for specified ClientId")
#   그래서 자격증명을 직접 지정한다:
#     · App Service : ManagedIdentityCredential() → system-assigned MI
#                     (AppServiceCredential은 AZURE_CLIENT_ID 를 읽지 않음 → 시스템 MI 사용)
#                     이 MI에 bybaek-foundry의 "Cognitive Services User" 역할이 부여돼 있어야 함.
#     · 로컬        : ManagedIdentityCredential 실패 → AzureCliCredential(az login) 로 폴백
_credential = ChainedTokenCredential(
    ManagedIdentityCredential(),
    AzureCliCredential(),
)
_token_provider = get_bearer_token_provider(_credential, _SCOPE)


def get_claude_token() -> str:
    """유효한 AAD Bearer 토큰 반환. 만료가 임박하면 내부적으로 자동 갱신된다."""
    return _token_provider()
