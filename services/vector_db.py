import os
import logging
from azure.cosmos import CosmosClient
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_COSMOS_URL")
KEY = os.getenv("AZURE_COSMOS_KEY")
DATABASE_NAME = "BybaekDB"
CONTAINER_NAME = "RagVectors"

VECTOR_TOP_K_CAP = 50  # 안전장치: 비정상적으로 큰 top_k 요청 방지

_container = None
if ENDPOINT and KEY:
    try:
        _client = CosmosClient(ENDPOINT, KEY)
        _database = _client.get_database_client(DATABASE_NAME)
        _container = _database.get_container_client(CONTAINER_NAME)
    except Exception as e:
        logging.error(f"[vector_db] Cosmos DB 벡터 컨테이너 연결 실패: {e}")
        _container = None
else:
    logging.warning("[vector_db] AZURE_COSMOS_URL/KEY 미설정 → vector container 비활성화")


def save_embedding(
    shop_id: str,
    post_id: str,
    caption: str,
    embedding: list,
    content_type: str = "caption_body",
    authored_by: str = "ai"
) -> bool:
    """
    캡션/해시태그/CTA/구조 패턴을 타입별로 Cosmos DB RagVectors 컨테이너에 저장.

    Args:
        shop_id: 상점 고유 식별자 (= 파티션 키)
        post_id: 게시물 고유 식별자
        caption: 저장할 텍스트
        embedding: 벡터 리스트 (1536차원, text-embedding-3-small)
        content_type: "caption_body" | "hashtag_set" | "cta" | "structure"

    Returns:
        bool: 저장 성공 여부
    """
    if not _container:
        logging.warning("[vector_db] container 없음 → 저장 스킵")
        return False

    document = {
        "id": f"{post_id}_{content_type}",
        "shop_id": shop_id,
        "caption": caption,
        "caption_vector": embedding,
        "content_type": content_type,
        "authored_by": authored_by,
    }

    try:
        _container.upsert_item(document)
        return True
    except Exception as e:
        logging.error(f"[vector_db] Cosmos DB 저장 실패: {e}")
        return False


def save_embeddings_batch(documents: list) -> bool:
    """
    여러 문서를 일괄 인덱싱 (cold start 백필용).
    documents: [{"id","shop_id","caption","caption_vector","content_type"}, ...]

    Cosmos DB Python SDK는 컨테이너 레벨 bulk API가 없으므로 순차 upsert로 처리.
    개별 실패는 로깅하고 계속 진행 (부분 성공 허용).
    """
    if not _container:
        logging.warning("[vector_db] container 없음 → 배치 저장 스킵")
        return False
    if not documents:
        return False

    success_count = 0
    for doc in documents:
        try:
            _container.upsert_item(doc)
            success_count += 1
        except Exception as e:
            logging.error(f"[vector_db] 배치 항목 저장 실패 (id={doc.get('id')}): {e}")

    logging.info(f"[vector_db] 배치 인덱싱 완료 → {success_count}/{len(documents)}개")
    return success_count > 0


def search_similar_captions(
    shop_id: str,
    query_vector: list,
    top_k: int = 5,
    query_text: str = None,
    content_type: str = None,
    authored_by: str = None
) -> list:
    """
    Cosmos DB VectorDistance()로 유사 캡션 검색 (코사인 유사도 기준).

    Args:
        shop_id (str): 검색 범위를 제한할 상점 식별자 (파티션 키)
        query_vector (list): 검색 기준 벡터 데이터
        top_k (int): 반환할 최상위 결과 수
        query_text (str): 미사용 (Azure Search의 BM25 키워드 검색 인자였음;
                           Cosmos DB 전환 후 순수 벡터 검색만 수행 — 하이브리드는 후속 과제)
        content_type (str): "caption_body" | "hashtag_set" | "cta" | "structure" 필터
        authored_by (str): "human"(사장님 원본) | "ai"(BYBAEK 생성분) 필터.
                           말투 학습용 caption_body는 human을 우선 조회해서,
                           AI가 자기 출력을 다시 학습하는 루프를 막는다.

    Returns:
        list: 유사도 높은 캡션 리스트 (score 포함), Azure Search 결과 형태와 호환되도록
              "id", "caption", "content_type", "@search.score" 키로 반환
    """
    if not _container:
        logging.warning("[vector_db] container 없음 → 빈 리스트 반환")
        return []

    safe_top_k = min(max(top_k, 1), VECTOR_TOP_K_CAP)

    query = (
        "SELECT TOP @top_k c.id, c.caption, c.content_type, "
        "VectorDistance(c.caption_vector, @embedding) AS score "
        "FROM c WHERE c.shop_id = @shop_id"
    )
    parameters = [
        {"name": "@top_k", "value": safe_top_k},
        {"name": "@embedding", "value": query_vector},
        {"name": "@shop_id", "value": shop_id},
    ]

    if content_type:
        query += " AND c.content_type = @content_type"
        parameters.append({"name": "@content_type", "value": content_type})

    if authored_by:
        query += " AND c.authored_by = @authored_by"
        parameters.append({"name": "@authored_by", "value": authored_by})

    query += " ORDER BY VectorDistance(c.caption_vector, @embedding)"

    try:
        results = _container.query_items(
            query=query,
            parameters=parameters,
            partition_key=shop_id,  # 단일 파티션 쿼리 → cross-partition 불필요, RU 절약
        )
        hits = []
        for r in results:
            hits.append({
                "id": r.get("id"),
                "caption": r.get("caption"),
                "content_type": r.get("content_type"),
                "@search.score": r.get("score", 0),  # 기존 rag_tool.py가 읽는 키 이름 유지
            })

        if hits:
            scores = [round(h["@search.score"], 4) for h in hits]
            logging.info(f"[vector_db] 검색 결과: {len(hits)}개, 유사도 점수: {scores}")
        else:
            logging.info(f"[vector_db] 검색 결과 없음 (shop_id={shop_id})")

        return hits

    except Exception as e:
        logging.error(f"[vector_db] 유사 캡션 검색 실패: {e}")
        return []
