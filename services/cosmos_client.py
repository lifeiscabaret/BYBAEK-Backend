#기능: Azure Cosmos DB 클라이언트 생성 및 컨테이너 연결 관리
#
# [#35] CosmosClient 싱글톤 캐시.
# - 매 호출마다 CosmosClient 를 새로 만들던 것을 프로세스당 1개로 재사용한다.
#   근거(공식 문서):
#     · "CosmosClient instance: One per app — Reuse for lifetime of app"
#       — Best practices for Python SDK, Azure Cosmos DB for NoSQL (learn.microsoft.com,
#         /azure/cosmos-db/nosql/best-practice-python, "SDK usage" 표)
#     · "It's recommended to maintain a single instance of CosmosClient per lifetime of
#        the application which enables efficient connection management and performance."
#       — azure.cosmos CosmosClient API 레퍼런스 (learn.microsoft.com/python/api/azure-cosmos)
#   thread-safety: 동기 CosmosClient 는 thread-safe 하다.
#     · ".NET/Java 공식 레퍼런스: 'CosmosClient is thread-safe.'" (동일 코어 동작)
#     · MS Q&A: "the CosmosClient instance is thread-safe ... good to use a single
#        instance for the lifetime." (/answers/questions/2112418)
#   주의(공식 경고): 동기 CosmosClient 를 async 이벤트루프 안에서 직접 쓰지 말 것
#     (blocking I/O). 본 프로젝트의 services/cosmos_db.py 는 전부 동기 함수이므로 해당
#     패턴에 부합하며, 이 변경은 기존 동기 사용법을 그대로 유지한다.
# - 스레드 안전: FastAPI 요청 핸들러 / APScheduler 잡(별도 스레드) / photo_queue_worker 가
#   동시에 접근하므로, 싱글톤 초기화는 threading.Lock 으로 이중 검사 잠금(double-checked)한다.
# - 환경변수 부재 시: 조용히 None 으로 넘어가지 않고 명확한 오류를 낸다.
# - 복원력: 끊긴 연결/키 교체를 위해, 연결/인증 오류로 실패하면 캐시를 버리고
#   (env 재로딩) 1회 재시도한다. 404/409 등 정상 업무 오류는 재생성하지 않는다.
#   get_cosmos_container 의 시그니처/반환 타입(컨테이너처럼 쓰는 객체)은 유지한다.

import os
import threading

from dotenv import load_dotenv
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.core.exceptions import ServiceRequestError, ServiceResponseError

load_dotenv()

DATABASE_NAME = "BybaekDB"

# ── 모듈 레벨 싱글톤 ───────────────────────────────────────────────────────────
_client: CosmosClient | None = None
_container_cache: dict[str, object] = {}
_lock = threading.Lock()

# 캐시 무효화 대상: 연결/인증/일시적 서비스 오류 (재생성해야 복구되는 것).
# 업무 오류(404 없음 / 409 충돌 / 412 조건불일치 / 429 과금제한)는 대상이 아니다.
_RESET_HTTP_STATUS = frozenset({401, 403, 408, 503})

# 프록시가 가로채 재시도를 붙이는 데이터 액세스 메서드들.
# [주의 — 재시도 보호 범위의 한계] query_items 는 '지연 평가' 이터레이터를 돌려준다.
#   즉 호출 시점이 아니라 결과를 순회할 때 실제 네트워크 I/O 가 일어난다. 본 프록시는
#   메서드 '호출' 시점의 예외만 감싸므로, 순회 도중 발생하는 연결 오류는 재시도되지 않는다.
#   이를 재시도하려면 결과 전체를 list() 로 미리 소비해야 하는데, 이는 대용량 결과의
#   스트리밍 이점을 없애고 메모리 거동/호출부 의미를 바꾼다(회귀 위험). 인프라가 안정적인
#   현 시점에는 과한 대응으로 판단하여, 부분 보호(호출 시점 + Cosmos SDK 자체 내장 재시도)를
#   유지하고 여기 명시만 한다. 필요 시 별도 이슈로 다룬다.
_PROXIED_METHODS = (
    "read_item", "create_item", "upsert_item", "replace_item", "delete_item",
    "query_items", "read_all_items", "patch_item",
)


def _read_env() -> tuple[str, str]:
    """환경변수를 읽고 검증한다. 없으면 명확한 오류(조용한 None 금지)."""
    endpoint = os.getenv("AZURE_COSMOS_URL")
    key = os.getenv("AZURE_COSMOS_KEY")
    missing = [n for n, v in (("AZURE_COSMOS_URL", endpoint), ("AZURE_COSMOS_KEY", key)) if not v]
    if missing:
        raise RuntimeError(
            f"Cosmos DB 환경변수 미설정: {', '.join(missing)}. "
            f"AZURE_COSMOS_URL / AZURE_COSMOS_KEY 를 설정해야 합니다."
        )
    return endpoint, key


def _get_client() -> CosmosClient:
    """캐시된 CosmosClient 를 반환. 없으면 이중 검사 잠금으로 1회만 생성."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:  # 잠금 획득 사이에 다른 스레드가 만들었을 수 있음
                endpoint, key = _read_env()
                _client = CosmosClient(endpoint, key)
    return _client


def _reset_client() -> None:
    """캐시된 클라이언트/컨테이너를 버린다. 다음 접근 시 env 를 다시 읽어 재생성됨.
    (끊긴 연결 복구 + 키 로테이션 반영)"""
    global _client
    with _lock:
        old = _client
        _client = None
        _container_cache.clear()
    if old is not None:
        try:
            old.close()  # 파이프라인 리소스 해제 (소켓/커넥션 누수 방지)
        except Exception:
            pass


def _raw_container(container_name: str):
    """캐시된 클라이언트에서 컨테이너 프록시(원본)를 얻는다. 이름별 캐시.

    주의: 클라이언트 획득(_get_client)은 반드시 컨테이너 잠금 '밖에서' 수행한다.
    _get_client 는 내부적으로 같은 _lock 을 잡을 수 있는데, _lock 은 재진입 불가
    (threading.Lock)이므로 잠금을 쥔 채 호출하면 교착 상태가 된다.
    """
    cont = _container_cache.get(container_name)
    if cont is None:
        client = _get_client()  # 컨테이너 잠금 밖에서 먼저 획득 (잠금 중첩 방지 = 데드락 방지)
        with _lock:
            cont = _container_cache.get(container_name)
            if cont is None:
                cont = client.get_database_client(DATABASE_NAME).get_container_client(container_name)
                _container_cache[container_name] = cont
    return cont


def _should_reset(exc: Exception) -> bool:
    """이 오류가 '캐시를 버리고 재생성하면 나아질' 성격인지 판단.
    연결/일시적 서비스 오류·인증 오류 → True. 정상 업무 오류(404/409/412/429) → False."""
    if isinstance(exc, (ServiceRequestError, ServiceResponseError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, CosmosHttpResponseError):
        return getattr(exc, "status_code", None) in _RESET_HTTP_STATUS
    return False


class _ResilientContainer:
    """get_cosmos_container 가 반환하는 얇은 프록시.

    컨테이너처럼 동작(모든 속성/메서드 위임)하되, 데이터 액세스 메서드는 감싸서
    연결/인증 오류가 나면 싱글톤을 리셋(env 재로딩)하고 딱 1회 재시도한다.
    무한 재생성 루프를 막기 위해 재시도는 1회로 제한한다.
    """

    __slots__ = ("_name",)

    def __init__(self, container_name: str):
        object.__setattr__(self, "_name", container_name)

    def _call_with_retry(self, method_name: str, *args, **kwargs):
        cont = _raw_container(self._name)
        try:
            return getattr(cont, method_name)(*args, **kwargs)
        except Exception as exc:
            if not _should_reset(exc):
                raise  # 업무 오류 등은 그대로 전파 (의미없는 재생성 방지)
            # 연결/인증 오류 → 캐시 버리고 env 다시 읽어 재생성 후 1회 재시도
            _reset_client()
            cont2 = _raw_container(self._name)
            return getattr(cont2, method_name)(*args, **kwargs)

    def __getattr__(self, name):
        # 데이터 액세스 메서드는 재시도 래핑, 그 외 속성/메서드는 원본에 그대로 위임.
        if name in _PROXIED_METHODS:
            def _wrapped(*args, **kwargs):
                return self._call_with_retry(name, *args, **kwargs)
            return _wrapped
        return getattr(_raw_container(self._name), name)


def get_cosmos_container(container_name: str):
    """
    Cosmos DB 컨테이너 클라이언트를 반환합니다.

    [#35] 내부적으로 CosmosClient 를 프로세스당 1개로 캐시·재사용합니다.
    반환 객체는 기존과 동일하게 read_item/query_items/upsert_item/delete_item 등을
    호출해 쓰면 되며(시그니처·사용법 불변), 연결/인증 오류 시 자동으로 클라이언트를
    재생성해 1회 재시도합니다.

    Args:
        container_name (str): 접근할 컨테이너 이름 (Shop, Photo, Post, Cache 등)

    Returns:
        컨테이너처럼 동작하는 프록시 객체 (ContainerProxy 호환)
    """
    return _ResilientContainer(container_name)
