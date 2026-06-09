"""
기능   : Cosmos DB Photo 컨테이너의 blob_url에서 SAS 토큰(? 이후) 제거
대상   : save_photo 수정 이전에 저장된 SAS URL 포함 데이터
실행법 : python migrate_blob_urls.py [--shop-id <id>] [--execute]
옵션   :
    --shop-id   특정 샵만 대상 (생략 시 전체 샵)
    --execute   실제 DB 수정 (생략 시 dry-run, 대상 목록만 출력)

주요 흐름:
    1. Photo 컨테이너 전체(또는 특정 shop_id) 조회
    2. blob_url에 '?' 포함된 doc 필터링
    3. dry-run: 대상 목록 출력만
       execute : blob_url 정리 후 upsert
"""
import sys
import os
import logging
from dotenv import load_dotenv

load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


def clean_blob_url(url: str) -> str:
    """SAS 토큰(? 이후) 제거하여 순수 URL 반환"""
    return url.split("?")[0]


def run_migration(shop_id: str = None, dry_run: bool = True):
    """
    Args:
        shop_id : 특정 샵만 처리 (None이면 전체)
        dry_run : True면 수정 없이 대상 목록만 출력
    """
    try:
        from services.cosmos_client import get_cosmos_container
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from services.cosmos_client import get_cosmos_container

    container = get_cosmos_container("Photo")

    # 1. 쿼리
    if shop_id:
        query = "SELECT * FROM c WHERE c.shop_id = @shop_id"
        parameters = [{"name": "@shop_id", "value": shop_id}]
        photos = list(container.query_items(
            query=query,
            parameters=parameters,
            enable_cross_partition_query=True
        ))
        log.info(f"조회 완료 — shop_id: {shop_id}, 총 {len(photos)}장")
    else:
        query = "SELECT * FROM c"
        photos = list(container.query_items(
            query=query,
            enable_cross_partition_query=True
        ))
        log.info(f"조회 완료 — 전체 샵, 총 {len(photos)}장")

    # 2. SAS 포함 doc 필터링
    targets = [p for p in photos if "?" in p.get("blob_url", "")]
    log.info(f"SAS 토큰 포함 doc: {len(targets)}건 / 전체 {len(photos)}건")

    if not targets:
        log.info("✅ 정리할 데이터 없음 — migration 완료")
        return

    # 3. 미리보기 출력 
    log.info("── 대상 목록 (상위 10건) ──")
    for doc in targets[:10]:
        old = doc.get("blob_url", "")
        new = clean_blob_url(old)
        log.info(f"  photo_id: {doc.get('id')}")
        log.info(f"    BEFORE: {old[:80]}...")
        log.info(f"    AFTER : {new}")

    if len(targets) > 10:
        log.info(f"  ... 외 {len(targets) - 10}건")

    # 4. dry-run 종료 or 실제 수정 
    if dry_run:
        log.info(f"\n[DRY-RUN] 실제 수정 없음. {len(targets)}건 수정 예정.")
        log.info("실제 반영하려면 --execute 옵션으로 재실행하세요.")
        return

    #  5. 실제 upsert 
    success = 0
    fail = 0

    for doc in targets:
        try:
            doc["blob_url"] = clean_blob_url(doc["blob_url"])
            container.upsert_item(body=doc)
            success += 1
        except Exception as e:
            log.error(f"  upsert 실패 (photo_id: {doc.get('id')}): {e}")
            fail += 1

    log.info(f"\n✅ migration 완료 — 성공: {success}건 / 실패: {fail}건")


# CLI 진입점
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Cosmos DB Photo 컨테이너 blob_url SAS 토큰 제거 migration"
    )
    parser.add_argument(
        "--shop-id",
        type=str,
        default=None,
        help="특정 shop_id만 처리 (생략 시 전체 샵)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제 DB 수정 실행 (생략 시 dry-run)"
    )
    args = parser.parse_args()

    dry_run = not args.execute

    if dry_run:
        log.info("=== DRY-RUN 모드 (실제 수정 없음) ===")
    else:
        log.info("=== EXECUTE 모드 (DB 실제 수정) ===")

    run_migration(shop_id=args.shop_id, dry_run=dry_run)