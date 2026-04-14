import os
import logging
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError
from datetime import datetime, timezone, timedelta
from azure.storage.blob import generate_blob_sas, BlobSasPermissions

AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.getenv("AZURE_BLOB_CONTAINER_NAME")

def generate_sas_url(blob_url: str, expiry_hours: int = 1) -> str:
    """
    Blob URL에서 SAS URL을 생성합니다.
    Instagram API가 이미지를 다운로드할 수 있도록 임시 접근 권한 부여.
    """
    try:
        # blob_url에서 파일명 추출 (SAS 토큰 제거)
        clean_url = blob_url.split("?")[0]
        file_name = clean_url.split("/")[-1]

        blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
        account_name = blob_service_client.account_name
        account_key = blob_service_client.credential.account_key

        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=CONTAINER_NAME,
            blob_name=file_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
        )

        return f"https://{account_name}.blob.core.windows.net/{CONTAINER_NAME}/{file_name}?{sas_token}"

    except Exception as e:
        logging.error(f"SAS URL 생성 실패 ({blob_url}): {str(e)}")
        return blob_url

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