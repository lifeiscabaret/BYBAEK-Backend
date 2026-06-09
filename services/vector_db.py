import os
import logging
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorizedQuery
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
KEY = os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME = os.getenv("AZURE_SEARCH_INDEX_NAME")

search_client = None
if ENDPOINT and KEY and INDEX_NAME:
    search_client = SearchClient(
        endpoint=ENDPOINT,
        index_name=INDEX_NAME,
        credential=AzureKeyCredential(KEY)
    )
else:
    logging.warning("[vector_db] Azure Search 환경변수 미설정 → search_client 비활성화")

def save_embedding(
    shop_id: str,
    post_id: str,
    caption: str,
    embedding: list,
    content_type: str = "caption_body"
) -> bool:
    """
    캡션/해시태그/CTA/구조 패턴을 타입별로 Vector DB에 저장.

    Args:
        shop_id: 상점 고유 식별자
        post_id: 게시물 고유 식별자
        caption: 저장할 텍스트
        embedding: 벡터 리스트
        content_type: "caption_body" | "hashtag_set" | "cta" | "structure"

    Returns:
        bool: 저장 성공 여부
    """
    if not search_client:
        logging.warning("[vector_db] search_client 없음 → 저장 스킵")
        return False

    document = {
        "id": post_id,
        "shop_id": shop_id,
        "caption": caption,
        "caption_vector": embedding,
        "content_type": content_type   
    }

    try:
        search_client.upload_documents(documents=[document])
        return True
    except Exception as e:
        logging.error(f"Vector DB 저장 실패: {str(e)}")
        return False

def search_similar_captions(shop_id: str, query_vector: list, top_k: int = 5, query_text: str = None, content_type: str = None) -> list:
    """
    Hybrid Search (Vector + BM25 키워드) 로 유사 캡션 검색.

    Args:
        shop_id (str): 검색 범위를 제한할 상점 식별자
        query_vector (list): 검색 기준 벡터 데이터
        top_k (int): 반환할 최상위 결과 수
        query_text (str): BM25 키워드 검색 텍스트 (없으면 vector only)

    Returns:
        list: 유사도 높은 캡션 리스트 (@search.score 포함)
    """
    if not search_client:
        logging.warning("[vector_db] search_client 없음 → 빈 리스트 반환")
        return []

    vector_query = VectorizedQuery(
        vector=query_vector,
        k_nearest_neighbors=top_k,
        fields="caption_vector"
    )

    try:
        # content_type 필터 조합
        base_filter = f"shop_id eq '{shop_id}'"
        if content_type:
            search_filter = f"{base_filter} and content_type eq '{content_type}'"
        else:
            search_filter = base_filter

        results = search_client.search(
            search_text=query_text,
            vector_queries=[vector_query],
            filter=search_filter,
            select=["id", "caption", "content_type"]
        )
        hits = list(results)

        # 유사도 점수 로깅
        if hits:
            scores = [round(h.get("@search.score", 0), 4) for h in hits]
            logging.info(f"[vector_db] Hybrid 검색 결과: {len(hits)}개, 유사도 점수: {scores}")
        else:
            logging.info(f"[vector_db] 검색 결과 없음 (shop_id={shop_id})")

        return hits

    except Exception as e:
        logging.error(f"유사 캡션 검색 실패: {str(e)}")
        return []