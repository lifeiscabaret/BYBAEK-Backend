from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os
from dotenv import load_dotenv
from routers import auth, onboarding, agent, schedule, instagram, photos


load_dotenv()


# ── 앱 시작/종료 시 실행할 로직 (DB 연결 등)
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[BYBAEK] 서버 시작")
    yield
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
        os.getenv("FRONTEND_URL", "http://localhost:3000")  # 배포 URL
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


# ── 헬스체크 (Azure App Service 배포 확인용)
@app.get("/", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "BYBAEK API"}