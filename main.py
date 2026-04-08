from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import os
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from routers import auth, onboarding, agent, schedule, instagram, photos, onedrive, custom_chat

load_dotenv()

KST = timezone(timedelta(hours=9))
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


async def _check_and_run_schedules():
    """
    매 정각마다 실행 → DB에서 지금 올릴 샵 찾아서 파이프라인 실행
    insta_upload_time이 현재 시각(HH:00)과 일치하는 샵만 실행
    """
    from services.cosmos_db import get_all_shops
    from agents.orchestrator import run_pipeline

    now = datetime.now(KST)
    current_time = now.strftime("%H:00")
    print(f"[scheduler] 스케줄 체크 → {current_time} (KST)")

    try:
        shops = get_all_shops()
    except Exception as e:
        print(f"[scheduler] 샵 목록 조회 실패: {e}")
        return

    for shop in shops:
        upload_time = shop.get("insta_upload_time", "")
        if upload_time != current_time:
            continue

        # 자동 업로드 OFF인 샵은 스킵
        if shop.get("insta_auto_upload_yn", "N") != "Y":
            continue

        shop_id = shop.get("id") or shop.get("shop_id")
        if not shop_id:
            continue

        print(f"[scheduler] 파이프라인 실행 → shop_id={shop_id}, time={current_time}")
        try:
            await run_pipeline(shop_id=shop_id, trigger="auto")
        except Exception as e:
            print(f"[scheduler] 파이프라인 실패 ({shop_id}): {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 매 정각마다 스케줄 체크
    scheduler.add_job(
        _check_and_run_schedules,
        CronTrigger(minute=0),
        id="auto_upload",
        replace_existing=True
    )
    scheduler.start()
    print("[BYBAEK] 서버 시작 + 스케줄러 ON")
    yield
    scheduler.shutdown()
    print("[BYBAEK] 서버 종료")


app = FastAPI(
    title="BYBAEK API",
    description="바버샵 AI 마케팅 자동화 에이전트 API",
    version="1.0.0",
    lifespan=lifespan
)

# ── CORS 설정 (Next.js 프론트엔드 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        os.getenv("FRONTEND_URL", "http://localhost:3000")
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우터 등록
app.include_router(auth.router,        prefix="/api/auth",        tags=["Auth"])
app.include_router(onboarding.router,  prefix="/api/onboarding",  tags=["Onboarding"])
app.include_router(agent.router,       prefix="/api/agent",       tags=["Agent"])
app.include_router(schedule.router,    prefix="/api/schedule",    tags=["Schedule"])
app.include_router(instagram.router,   prefix="/api/instagram",   tags=["Instagram"])
app.include_router(photos.router,      prefix="/api/photos",      tags=["Photos"])
app.include_router(onedrive.router,    prefix="/api/onedrive",    tags=["Onedrive"])
app.include_router(custom_chat.router, prefix="/api/custom_chat", tags=["CustomChat"])

# ── 헬스체크 (Azure App Service 배포 확인용)
@app.get("/", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "BYBAEK API"}