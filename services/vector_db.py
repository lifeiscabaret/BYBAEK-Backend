"""
기능: Azure AI Search를 이용한 벡터 데이터 저장 및 유사도 검색
작성자: jiyeon back
최초 생성: 2026. 03. 04.
버전: 1.0

[Modification Information]
DATE        AUTHOR          NOTE
-----------------------------------------------------------
2026.03.04  jiyeon back     최초 생성 및 벡터 검색 로직 구현
"""

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

if not ENDPOINT or not KEY or not INDEX_NAME:
    raise ValueError("Azure Search 환경변수가 설정되지 않았습니다.")

search_client = SearchClient(
    endpoint=ENDPOINT,
    index_name=INDEX_NAME,
    credential=AzureKeyCredential(KEY)
)

def save_embedding(shop_id: str, post_id: str, caption: str, embedding: list) -> bool:
    """
    생성된 캡션과 해당 캡션의 벡터 데이터를 AI Search 인덱스에 업로드합니다.

    Args:
        shop_id (str): 상점 고유 식별자
        post_id (str): 게시물 고유 식별자
        caption (str): 벡터화된 원문 캡션
        embedding (list): AI 모델을 통해 추출된 수치 벡터 리스트

    Returns:
        bool: 저장 성공 여부
    """
    document = {
        "id": post_id,
        "shop_id": shop_id,
        "caption": caption,
        "caption_vector": embedding  
    }
    
    try:
        search_client.upload_documents(documents=[document])
        return True
    except Exception as e:
        logging.error(f"Vector DB 저장 실패: {str(e)}")
        return False

def search_similar_captions(shop_id: str, query_vector: list, top_k: int = 5) -> list:
    """
    입력된 쿼리 벡터와 가장 유사한 기존 캡션들을 AI Search에서 검색합니다.

    Args:
        shop_id (str): 검색 범위를 제한할 상점 식별자
        query_vector (list): 검색 기준이 되는 벡터 데이터
        top_k (int): 검색 결과로 반환할 최상위 결과 수

    Returns:
        list: 유사도가 높은 캡션 데이터 리스트
    """
    vector_query = VectorizedQuery(
        vector=query_vector, 
        k_nearest_neighbors=top_k, 
        fields="caption_vector"
    )

    try:
        results = search_client.search(
            search_text=None,
            vector_queries=[vector_query],
            filter=f"shopId eq '{shop_id}'",
            select=["id", "caption"]
        )
        return list(results)
    except Exception as e:
        logging.error(f"유사 캡션 검색 실패: {str(e)}")
        return []