#기능: Azure Cosmos DB 클라이언트 생성 및 컨테이너 연결 관리
import os
from dotenv import load_dotenv
from azure.cosmos import CosmosClient

load_dotenv()

def get_cosmos_container(container_name: str):
    """
    Cosmos DB 클라이언트를 생성하고 지정된 컨테이너 객체를 반환합니다.

    Args:
        container_name (str): 접근할 컨테이너 이름 (ShopInfo, PhotoAlbum 등)

    Returns:
        ContainerProxy: Azure Cosmos DB 컨테이너 클라이언트 객체
    """
    # 환경 변수 로드
    endpoint = os.getenv("AZURE_COSMOS_URL")
    key = os.getenv("AZURE_COSMOS_KEY")
    database_name = "BybaekDB"

    # 클라이언트 및 데이터베이스 연결
    client = CosmosClient(endpoint, key)
    database = client.get_database_client(database_name)
    
    # 해당 컨테이너 클라이언트 반환
    container = database.get_container_client(container_name)

    return container