from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import logging

from services.cosmos_db import get_shop_info, update_schedule_settings

router = APIRouter()
logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


class ScheduleUpdate(BaseModel):
    upload_time: str      # 형식: "19:00"
    timezone: str = "Asia/Seoul"


@router.get("/{shop_id}")
async def get_schedule(shop_id: str):
    """
    [조회] 사장님이 설정한 인스타 자동 업로드 시간 및 다음 실행 예정 시간 확인
    """
    try:
        shop_info = get_shop_info(shop_id)

        if not shop_info:
            return {
                "upload_time": "19:00",
                "next_upload": _calculate_next_run("19:00"),
                "timezone": "Asia/Seoul",
                "message": "기본 설정값입니다."
            }

        upload_time = shop_info.get("insta_upload_time", "19:00")

        return {
            "upload_time": upload_time,
            "next_upload": _calculate_next_run(upload_time),
            "timezone": "Asia/Seoul"
        }

    except Exception as e:
        logger.error(f"스케줄 조회 중 에러: {e}")
        raise HTTPException(status_code=500, detail="스케줄 정보를 가져오지 못했습니다.")


@router.post("/{shop_id}/update")
async def update_schedule(shop_id: str, req: ScheduleUpdate):
    """
    [수정] 사장님이 앱에서 업로드 예약 시간을 변경할 때 호출
    """
    try:
        success = update_schedule_settings(
            shop_id=shop_id,
            upload_time=req.upload_time,
            timezone=req.timezone
        )

        if not success:
            raise Exception("DB 업데이트 실패")

        return {
            "status": "success",
            "message": f"업로드 시간이 {req.upload_time}으로 변경됐어요.",
            "next_upload": _calculate_next_run(req.upload_time)
        }

    except Exception as e:
        logger.error(f"스케줄 업데이트 중 에러: {e}")
        raise HTTPException(status_code=500, detail=f"업데이트 실패: {str(e)}")


def _calculate_next_run(upload_time_str: str) -> str:
    try:
        now = datetime.now(KST)
        hour, minute = map(int, upload_time_str.split(":"))

        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if next_run <= now:
            next_run += timedelta(days=1)

        return next_run.isoformat()

    except Exception:
        return (datetime.now(KST) + timedelta(days=1)).isoformat()