from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
import asyncio
import os
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from routers import auth, onboarding, agent, schedule, instagram, photos, onedrive, custom_chat
from workers.photo_queue_worker import start_worker


load_dotenv()

KST = timezone(timedelta(hours=9))
scheduler = AsyncIOScheduler(timezone="Asia/Seoul")


# 정각에 동시에 돌릴 파이프라인 수.
# 파이프라인 1회가 웹서치+Claude+재시도까지 수십 초라, 예전처럼 순차 실행하면
# 같은 시간대 샵이 몇 개만 몰려도 뒤쪽 샵이 다음 정각으로 밀렸다.
# 무제한 동시 실행은 Azure OpenAI / Claude rate limit에 걸리므로 상한을 둔다.
PIPELINE_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "4"))


async def _run_pipeline_guarded(shop_id: str, sem: "asyncio.Semaphore"):
    """샵 1개 파이프라인 실행. 한 샵 실패가 다른 샵을 막지 않도록 예외를 격리한다."""
    from orchestrator_v2 import run_pipeline
    async with sem:
        try:
            await run_pipeline(shop_id=shop_id, trigger="auto")
        except Exception as e:
            print(f"[scheduler] 파이프라인 실패 ({shop_id}): {e}")


async def _check_and_run_schedules():
    from services.cosmos_db import get_all_shops

    now = datetime.now(KST)
    current_hour = now.strftime("%H:00")
    print(f"[scheduler] 스케줄 체크 → {current_hour} (KST)")

    try:
        shops = get_all_shops()  # ← 추가
    except Exception as e:
        print(f"[scheduler] 샵 목록 조회 실패: {e}")
        return

    due_shop_ids = []
    for shop in shops:
        upload_time = shop.get("insta_upload_time", "")
        if not upload_time:
            continue

        try:
            from datetime import datetime as dt
            parsed = dt.strptime(upload_time.strip(), "%I:%M %p")
            upload_time_24h = parsed.strftime("%H:00")
        except:
            upload_time_24h = upload_time

        if upload_time_24h != current_hour:
            continue

        if shop.get("insta_auto_upload_yn", "N") != "Y":
            continue

        # 업로드 빈도 체크 — 시각이 맞아도 요일이 안 맞으면 스킵.
        # DB의 insta_upload_days(사용자 지정 요일)를 우선 사용하고,
        # 비어 있으면 time_slot 기반 기본 요일(주3회=월·수·금, 주1회=월)로 폴백.
        upload_days   = shop.get("insta_upload_days", []) or []
        time_slot     = (shop.get("insta_upload_time_slot") or "매일").strip()
        today_weekday = now.weekday()  # 월=0 ... 일=6

        if time_slot == "매일" or not time_slot:
            should_run = True
        elif time_slot in ("주 3회", "주 1회"):
            if upload_days:
                should_run = today_weekday in upload_days
            else:
                # 설정 안 됐으면 기본값 사용
                should_run = today_weekday in ([0, 2, 4] if time_slot == "주 3회" else [0])
        else:
            should_run = True

        if not should_run:
            continue

        shop_id = shop.get("id") or shop.get("shop_id")
        if not shop_id:
            continue

        due_shop_ids.append(shop_id)

    if not due_shop_ids:
        print(f"[scheduler] {current_hour} 실행 대상 샵 없음")
        return

    print(f"[scheduler] 파이프라인 실행 → {len(due_shop_ids)}개 샵, "
          f"동시 {PIPELINE_CONCURRENCY}개, time={current_hour}")
    sem = asyncio.Semaphore(PIPELINE_CONCURRENCY)
    await asyncio.gather(*(_run_pipeline_guarded(sid, sem) for sid in due_shop_ids))
    print(f"[scheduler] {current_hour} 배치 완료 ({len(due_shop_ids)}개 샵)")


async def _sync_all_shops_onedrive():
    """모든 샵의 OneDrive 사진 자동 동기화"""
    from services.cosmos_db import get_all_shops
    from routers.onedrive import sync_photos_internal

    shops = get_all_shops()
    for shop in shops:
        shop_id = shop.get("shop_id")
        if not shop_id:
            continue
        try:
            await sync_photos_internal(shop_id)
            print(f"[scheduler] OneDrive 동기화 완료 → shop_id={shop_id}")
        except Exception as e:
            print(f"[scheduler] OneDrive 동기화 실패 → shop_id={shop_id}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 매 정각마다 스케줄 체크
    scheduler.add_job(
        _check_and_run_schedules,
        CronTrigger(minute=0),
        id="auto_upload",
        replace_existing=True
    )
    # 4시간마다 OneDrive 자동 동기화
    scheduler.add_job(
        _sync_all_shops_onedrive,
        CronTrigger(minute=0, hour="*/4"),
        id="onedrive_sync",
        replace_existing=True
    )
    # 매일 새벽 4시 인스타 장기 토큰 갱신 (60일 만료 → 만료 전 자동 연장)
    from workers.insta_token_refresh import refresh_all_instagram_tokens
    scheduler.add_job(
        refresh_all_instagram_tokens,
        CronTrigger(hour=4, minute=10),
        id="insta_token_refresh",
        replace_existing=True
    )
    scheduler.start()
    start_worker()  # ✅ lifespan 안으로 이동 (on_event 대체)
    print("[BYBAEK] 서버 시작 + 스케줄러 ON + 큐 워커 ON")
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
        *[o.strip() for o in os.getenv("FRONTEND_URL", "http://localhost:3000").split(",") if o.strip()]
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