# BYBAEK — AI 마케팅 자동화 SaaS · Backend

> 소상공인 바버샵 사장님의 인스타그램 마케팅을 AI로 완전 자동화하는 B2B SaaS  
> **Microsoft Azure Marketplace 출시 준비 중 (1차 MVP)**

[![Python](https://img.shields.io/badge/Python-3.11-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)](https://fastapi.tiangolo.com)
[![Azure](https://img.shields.io/badge/Azure-OpenAI%20%7C%20CosmosDB-0078D4)](https://azure.microsoft.com)
[![Semantic Kernel](https://img.shields.io/badge/Semantic%20Kernel-AI%20Orchestration-purple)](https://github.com/microsoft/semantic-kernel)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## 📌 서비스 한 줄 소개

> 사장님이 OneDrive에 사진을 올리면, 실시간 트렌드를 반영한 인스타그램 게시물이  
> **설정 1회 + 승인 1회**로 자동 생성·업로드됩니다.

---

## 🏗 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (React Native)                   │
└─────────────────────────────┬───────────────────────────────────┘
                              │ API 요청/응답
┌─────────────────────────────▼───────────────────────────────────┐
│                     Backend (FastAPI / Azure Functions)          │
│  auth.py │ onboarding.py │ agent.py │ schedule.py │ instagram.py │
└──────────┬────────────────────────────────────────┬─────────────┘
           │                                        │
┌──────────▼───────────┐              ┌─────────────▼─────────────┐
│     AI Orchestrator   │              │      Azure Services        │
│  orchestrator.py      │              │  CosmosDB │ Blob Storage   │
│  web_search.py        │◄────────────►│  AI Search │ AI Vision     │
│  photo_select.py      │              │  OpenAI GPT-4.1            │
│  post_writer.py       │              └───────────────────────────┘
│  rag_tool.py          │
└──────────┬────────────┘
           │
┌──────────▼────────────┐
│    External APIs       │
│  MS Graph (OneDrive)   │
│  Instagram Graph API   │
│  Tavily (트렌드 검색)  │
└───────────────────────┘
```

---

## 🛠 기술 스택

| 분류 | 기술 | 용도 |
|---|---|---|
| **Backend** | Python 3.11, FastAPI, Gunicorn/Uvicorn | 서버리스 API 서버 |
| **AI 오케스트레이션** | Semantic Kernel, Azure OpenAI GPT-4.1 / GPT-4.1-mini | Multi-agent 파이프라인 |
| **RAG / Vector DB** | Azure AI Search, text-embedding-3-small | 브랜드 캡션 학습 및 검색 |
| **데이터베이스** | Azure CosmosDB (NoSQL) | 브랜드 설정, 게시물 이력 |
| **스토리지** | Azure Blob Storage | 시술 사진 원본 저장 |
| **이미지 분석** | Azure AI Vision | 페이드컷 선명도/밝기/흔들림 자동 분석 |
| **사진 수집** | MS Graph API (OneDrive) | 사장님 OneDrive 자동 동기화 |
| **SNS 업로드** | Instagram Graph API | 게시물 자동 업로드 및 예약 |
| **인증** | Microsoft OAuth 2.0 | MS 계정 원클릭 로그인 |
| **트렌드 수집** | Tavily API | 실시간 바버샵 헤어 트렌드 수집 |
| **배포** | Azure App Service (Korea Central) | 풀스택 배포 |

---

## 🤖 AI 에이전트 파이프라인 (STEP 0~6)

```
STEP 0   Tiered Routing
         요청 복잡도(trigger 유형 + 사진 수) 분류
         → auto + 사진 5장 미만 : GPT-4.1-mini
         → manual OR 사진 5장 이상 : GPT-4.1

STEP 1   병렬 수집 (asyncio)
         웹서치(Tavily) + 브랜드설정 + 사진후보 + 최근게시물 동시 호출

STEP 2   트렌드 품질 게이팅
         trend_score < 0.7 → 재시도(MAX_RETRY=2) → 소진 시 full 모델 승격

STEP 3   Photo Select Agent
         각도 분류(페이드/스타일링/분위기) + 14일 재사용 쿨다운

STEP 4   RAG Tool
         Vector DB 검색 → GPT로 컨텍스트 압축
         → tone_rules / examples / hashtag_patterns 추출
         → Fallback: 결과 없을 시 recent_posts 기반 컨텍스트

STEP 5   Post Writer + LLM-as-Judge
         캡션 + 해시태그 + CTA 생성
         caption_score < 0.7 → 자동 재시도 → 소진 시 full 모델 승격
         실측 caption_score 평균 0.86 / 운영 중 retry 0회

STEP 6   Human-in-the-loop 분기
         설정값에 따라 자동업로드 OR 사장님 검토(OK/수정/취소) 분기
```

---

## 🧠 핵심 설계 포인트

### 1. Hallucination 3중 제어
```
① 프롬프트 가드레일  : "경력 연수·예약 현황 절대 언급 금지" 명시
② 후처리 정규식     : \d+년 경력 / \d+자리 남 패턴 자동 감지
③ feedback 재시도   : 감지 시 해당 패턴 명시하여 재생성 요청
```

### 2. RAG — 단순 LLM 호출이 아닌 브랜드 학습 구조
- 매 호출마다 브랜드 톤을 프롬프트에 직접 주입하면 컨텍스트 윈도우 낭비
- 각 샵의 과거 게시물에서 추출한 **tone_rules, 말투 패턴, 해시태그 스타일**을 Vector DB에 축적
- 생성 시점에 해당 샵 컨텍스트만 검색해 주입 → "사장님이 직접 쓴 것 같은 말투" 재현

### 3. Cold Start 해결
- 신규 가입 샵 → 도메인 특화 seed 캡션 30개 수동 설계·주입
- Fallback: 유사도 낮을 시 recent_posts 기반 컨텍스트 자동 전환

### 4. State 관리
- 단계별 상태값 유지: JSON Schema 기반 Context 전달 체계
- 각 STEP 출력이 다음 STEP 입력으로 정확히 전달 → Context Drift 방지

---

## 📁 프로젝트 구조

```
BYBAEK-Backend/
├── agents/
│   ├── orchestrator.py      # 전체 파이프라인 조율
│   ├── web_search.py        # Tavily 트렌드 수집
│   ├── photo_filter.py      # Azure AI Vision 사진 필터링
│   ├── photo_select.py      # 최적 사진 선택 에이전트
│   ├── post_writer.py       # 캡션 생성 + Self-Eval Loop
│   └── rag_tool.py          # Vector DB 검색 + 컨텍스트 압축
├── services/
│   ├── vector_db.py         # Azure AI Search 연동
│   ├── cosmos_db.py         # CosmosDB CRUD
│   └── blob_storage.py      # Blob Storage 연동
├── api/
│   ├── auth.py              # MS OAuth 2.0
│   ├── onboarding.py        # 브랜드 설정 온보딩
│   ├── agent.py             # 파이프라인 트리거
│   ├── schedule.py          # 예약 업로드
│   └── instagram.py         # Instagram Graph API
├── seed_embeddings.py       # Cold Start 해결 seed 주입
└── requirements.txt
```

---

## 📊 실측 지표 (1차 MVP 기준)

| 지표 | 수치 |
|---|---|
| caption_score 평균 | **0.86** (임계값 0.7 대비 +23%) |
| 운영 중 retry 발생 | **0회** (첫 시도 통과율 100%) |
| 모델 비용 구조 | GPT-4.1-mini 우선 라우팅 (full 대비 약 80% 절감) |
| 외부 API 실연동 | **5개** (Instagram · Gmail · OneDrive · Tavily · Azure) |
| 배포 환경 | Azure App Service Korea Central |

---

## ⚙️ Technical Decision

| 결정 | 선택 | 이유 |
|---|---|---|
| LLM 프레임워크 | Semantic Kernel | Azure 네이티브 통합 + Planner 패턴으로 에이전트 단계별 제어 |
| 임베딩 모델 | text-embedding-3-small | GPT 임베딩 대비 5배 저렴, 품질 차이 미미 |
| 캐시 저장소 | CosmosDB TTL 1일 | 웹서치 중복 호출 차단, 기존 인프라 재활용 |
| 품질 임계값 | 0.7 | 반복 테스트로 "사람이 보기에 어색한 경계값" 직접 확인 |
| Human-in-the-loop | 설정값 분기 | 자동화 vs 안전성 트레이드오프를 사용자가 직접 제어 |

---

## 🚀 현재 상태 (1차 MVP)

### ✅ 완료
- Multi-step 에이전트 파이프라인 (STEP 0~6)
- RAG 구현 + seed 데이터 30개 주입
- Hallucination 3중 제어
- Tiered Routing + LLM-as-Judge 품질 게이팅
- 사진 필터링 (Azure AI Vision)
- Gmail 알림 연동
- Azure App Service 배포 (Frontend + Backend)

### 🔜 진행 중
- Meta Developer 인증 → Instagram 실 계정 연동
- OneDrive Easy Auth 헤더 문제 해결
- Hybrid Search 도입 (BM25 + Vector)
- RAG 플라이휠 (업로드 성공 캡션 자동 Vector DB 저장)
- **Microsoft Azure Marketplace 출시 준비 중**

---

## 🌍 글로벌 아키텍처

> Day 1 Global — 타임존 인식 스케줄링 + 영어 트렌드 수집 기본 적용  
> 🇰🇷 서울 · 🇺🇸 뉴욕 · 🇬🇧 런던 동일 엔진으로 즉시 서비스 가능

---

## 🤝 협력 관계

| 기관 | 협력 내용 |
|---|---|
| **Microsoft Korea** | Azure 기술 미팅 진행 · Azure Marketplace 출시 준비 중 |
| **IUI** | MS Azure Marketplace 배포 담당 · 출시 전 과정 공식 개발 협력사 |

---

## 👥 팀 역할

| 이름 | 역할 | 담당 작업 |
|---|---|---|
| **이지현** | Team Lead / AI Engineer / Backend | AI 에이전트 5종 설계 및 구현 · RAG 파이프라인 설계 및 Cold Start 해결 · Tiered Routing + LLM-as-Judge + Hallucination 3중 제어 구조 설계 · Azure App Service & Cosmos DB 운영 |
| **이태경** | Backend | OneDrive MS Graph API 1차 연동 · Instagram Graph API 1차 연동 |
| **차명근** | Frontend Engineer | React Native for Windows 전체 화면 UI/UX 구현 |
| **백지연** | DB Engineer | CosmosDB 스키마 설계 · CRUD 함수 구현 · Vector DB 연동 |

---

*Built with ❤️ on Azure · Team Ctrl+A · Microsoft Marketplace 런칭 목표*