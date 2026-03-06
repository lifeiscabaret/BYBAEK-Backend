import os
import logging
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_BLOB_CONTAINER_NAME")

def delete_blob(file_name: str) -> bool:
    """
    Azure Blob Storage에서 특정 파일을 삭제합니다.
    
    Args:
        file_name (str): 삭제할 파일명 (blob_url의 마지막 부분)
    """
    if not AZURE_STORAGE_CONNECTION_STRING:
        logging.error("Azure Storage 연결 문자열이 설정되지 않았습니다.")
        return False

    try:
        # 1. BlobServiceClient 생성
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        
        # 2. 삭제할 파일(Blob)에 대한 클라이언트 생성
        blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=file_name)
        
        # 3. 파일 삭제
        blob_client.delete_blob()
        logging.info(f"Blob 삭제 성공: {file_name}")
        return True

    except ResourceNotFoundError:
        logging.warning(f"삭제하려는 Blob이 이미 존재하지 않습니다: {file_name}")
        return True  # 이미 없으면 성공으로 간주
    except Exception as e:
        logging.error(f"Blob 삭제 실패 ({file_name}): {str(e)}")
        return False