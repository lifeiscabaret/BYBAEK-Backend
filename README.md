# BYBAEK — Backend

> 바버샵 AI 마케팅 자동화 에이전트 — FastAPI 백엔드

---

## Tech Stack

| 구분 | 기술 |
|---|---|
| Framework | Python FastAPI + Uvicorn |
| AI 에이전트 | Azure OpenAI (GPT-4.1-mini / GPT-4.1) + Semantic Kernel |
| DB | Azure Cosmos DB |
| 사진 저장 | Azure Blob Storage |
| 벡터 검색 | Azure AI Search |
| 사진 필터링 | Azure AI Vision (GPT-4o Vision) |
| 사진 수집 | Microsoft OneDrive (MS Graph API) |
| 인스타 업로드 | Instagram Graph API |
| 웹서치 | Tavily API |
| 배포 | Azure App Service |

---

## 역할 분담

| 팀원 | 담당 폴더 | 주요 작업 |
|---|---|---|
| 지현 (Team Lead) | `agents/` | AI 에이전트, RAG, 사진 필터링, 오케스트레이션 |
| 태경 (Tech Lead) | `routers/`, `services/blob_storage.py` | API 라우터, 로그인, 배포, 보안 |
| 지연 (DB) | `services/cosmos_db.py`, `services/vector_db.py` | Cosmos DB 스키마, CRUD |

---

## 폴더 구조

```
bybaek-backend/
├── main.py                  # FastAPI 앱 진입점
├── requirements.txt
├── .env.example             # 환경변수 템플릿 (이걸 .env로 복사해서 사용)
├── agents/
│   ├── orchestrator.py      # 에이전트 오케스트레이터 (지현)
│   ├── web_search.py        # 트렌드/날씨 웹서치 에이전트 (지현)
│   ├── photo_select.py      # 사진 선택 에이전트 (지현)
│   ├── photo_filter.py      # 사진 1차/2차 필터링 (지현)
│   ├── post_writer.py       # 게시물 작성 에이전트 (지현)
│   └── rag_tool.py          # RAG 컨텍스트 추출 (지현)
├── routers/
│   ├── auth.py              # MS OAuth 로그인 (태경)
│   ├── agent.py             # 에이전트 실행 트리거 (태경)
│   ├── onboarding.py        # 스무고개 저장/조회 (태경)
│   ├── schedule.py          # 업로드 스케줄 (태경)
│   └── instagram.py         # Instagram 업로드 (태경)
├── services/
│   ├── cosmos_db.py         # Cosmos DB CRUD (지연)
│   ├── vector_db.py         # Azure AI Search 벡터 검색 (지연)
│   └── blob_storage.py      # Blob Storage 업로드/삭제 (태경)
└── tests/
    ├── test_photo_select.py
    └── test_web_search.py
```

---

## 로컬 세팅 방법

### 0. Python 버전 확인 (3.11.9 고정)

```bash
python --version  # Python 3.11.9 이어야 함
```

pyenv로 버전 맞추기:
```bash
brew install pyenv
pyenv install 3.11.9
pyenv local 3.11.9
```

### 1. 가상환경 생성 및 활성화

```bash
python -m venv venv

# Mac
source venv/bin/activate

# Windows
venv\Scripts\activate
```

터미널 앞에 `(venv)` 뜨면 성공

### 2. 패키지 설치

```bash
pip install -r requirements.txt
```

### 3. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일 열고 팀 채널에서 받은 키값 입력

### 4. 서버 실행

```bash
uvicorn main:app --reload --port 8000
```

### 5. API 문서 확인

브라우저에서 열기:
```
http://localhost:8000/docs
```

---

## API 엔드포인트

| Method | URL | 설명 | 담당 |
|---|---|---|---|
| GET | `/api/auth/login` | MS OAuth 로그인 | 태경 |
| GET | `/api/auth/callback` | 토큰 교환 | 태경 |
| POST | `/api/onboarding` | 스무고개 저장 | 태경 |
| GET | `/api/onboarding/{shop_id}` | 온보딩 조회 | 태경 |
| POST | `/api/agent/run` | 에이전트 실행 | 태경 → 지현 |
| POST | `/api/agent/review` | 사장님 검토 결과 | 태경 |
| GET | `/api/schedule/{shop_id}` | 스케줄 조회 | 태경 |
| POST | `/api/instagram/upload` | 인스타 업로드 | 태경 |

---

## Branch 전략

```
main     → 최종 배포 (직접 push 금지, PR만)
dev      → 개발 통합 (모든 feature는 여기로 PR)
feature/ → 개인 작업 브랜치
```

작업 시작 전 반드시:
```bash
git checkout dev
git pull origin dev
git checkout -b feature/내작업이름
```

---

## Commit Message Convention

| Type | 설명 |
|---|---|
| `[FEAT]` | 새 기능 추가 |
| `[FIX]` | 버그 수정 |
| `[REFACTOR]` | 리팩토링 |
| `[DOCS]` | 문서 수정 |
| `[CHORE]` | 설정/패키지 변경 |
| `[TEST]` | 테스트 추가 |

예시:
```
[FEAT] Add photo filter agent with GPT-4o Vision
[FIX] Fix Cosmos DB connection timeout
[CHORE] Update requirements.txt
```

---

## 주의사항

- `.env` 파일 절대 GitHub에 올리지 않기
- `venv/` 폴더 절대 GitHub에 올리지 않기
- API 키는 반드시 팀 채널을 통해서만 공유
- 작업 시작 전 항상 `git pull origin dev` 먼저
- `main` 브랜치에 직접 push 금지
- PR은 최소 1명 리뷰 후 merge
