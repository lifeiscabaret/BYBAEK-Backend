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

def save_embedding(shop_id, post_id, caption, embedding):
    """
    AI Search 인덱스에 벡터 데이터를 저장합니다.
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

def search_similar_captions(shop_id, query_vector, top_k=5):
    """
    유사한 캡션을 벡터 검색으로 찾아옵니다.
    """
    # 벡터 쿼리 객체 생성
    vector_query = VectorizedQuery(
        vector=query_vector, 
        k_nearest_neighbors=top_k, 
        fields="caption_vector"
    )

    try:
        results = search_client.search(
            search_text=None,
            vector_queries=[vector_query],
            filter=f"shop_id eq '{shop_id}'", # 특정 상점 데이터만 필터링
            select=["id", "caption"]
        )
        return list(results)
    except Exception as e:
        logging.error(f"유사 캡션 검색 실패: {str(e)}")
        return []