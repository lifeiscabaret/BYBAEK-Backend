# feat/email 작업 기록

브랜치: `feat/email`
작업 근거: `featemail.md`
작업 일자: 2026-04-10
최종 업데이트: 2026-04-10 (이메일 액션 버튼 추가)

---

## 작업 목록

| # | 작업 | 파일 | 상태 |
|---|------|------|------|
| 1 | `get_post_detail_data` 응답 형식 수정 | `services/cosmos_db.py` | ✅ 완료 |
| 2 | `save_gmail_token` 함수 추가 | `services/cosmos_db.py` | ✅ 완료 |
| 3 | `agent_review` / `get_post_detail` 응답 형식 수정 | `routers/agent.py` | ✅ 완료 |
| 4 | `send_draft_notification` 업데이트 | `services/email_service.py` | ✅ 완료 |
| 5 | `_send_push_notification` 버그 수정 | `orchestrator_v2.py` | ✅ 완료 |
| 6 | Gmail OAuth 엔드포인트 추가 | `routers/auth.py` | ✅ 완료 |
| 7 | onboarding GET 응답 형식 수정 | `routers/onboarding.py` | ✅ 완료 |
| 8 | `email_service.py` 환경변수 기반 리팩토링 | `services/email_service.py` | ✅ 완료 |
| 9 | 이메일 발송 로컬 테스트 스크립트 작성 | `services/test_email.py` | ✅ 완료 |
| 10 | 이메일 내 승인/수정/거절 버튼 추가 | `services/email_service.py`, `routers/agent.py` | ✅ 완료 |

---

## 작업 상세

### 작업 1 — `get_post_detail_data` 응답 형식 수정

- **파일:** `services/cosmos_db.py`
- **함수:** `get_post_detail_data(post_id, shop_id)`
- **위치:** 447~489줄

#### 문제점
| 항목 | 기존 | 요구 |
|------|------|------|
| 사진 배열 키 | `photo_details` | `photos` |
| 사진 항목 필드 | `id`, `blob_url` | `id`, `original_name`, `blob_url` |
| 게시물 ID 필드 | 없음 (`id`만 존재) | `post_id` |
| 상태값 | `"pending"` | `"pending_review"` |
| 응답 구조 | CosmosDB 원본 객체 그대로 반환 | 프론트 요구 형식으로 가공 |

#### 변경 내용
- `photo_container.read_item()`으로 `original_name` 추가 조회
- 반환 딕셔너리를 프론트 요구 스펙으로 재구성:
  ```json
  {
    "post_id": "post_abc123",
    "shop_id": "shop_123",
    "caption": "...",
    "photos": [{"id": "...", "original_name": "file.jpg", "blob_url": "https://..."}],
    "status": "pending_review",
    "hashtags": [...],
    "cta": "..."
  }
  ```
- `status == "pending"` → `"pending_review"` 매핑 추가
- 하위 호환을 위해 `hashtags`, `cta`, `metrics` 함께 포함

#### 완료 시각
✅ 2026-04-10

---

### 작업 2 — `save_gmail_token` 함수 추가

- **파일:** `services/cosmos_db.py`
- **위치:** `get_post_detail_data` 바로 아래 신규 삽입

#### 추가 내용
Gmail OAuth 콜백에서 호출할 함수. Shop 컨테이너에 아래 필드를 upsert:
```python
shop_item["owner_email"] = email
shop_item["is_gmail_connected"] = True
shop_item["gmail_access_token"] = access_token
shop_item["gmail_refresh_token"] = refresh_token
shop_item["gmail_connected_at"] = datetime.utcnow().isoformat()
```
- Shop 문서가 없으면 신규 생성 후 저장
- 이 함수는 `routers/auth.py` Gmail 콜백 엔드포인트에서 호출됨

#### 완료 시각
✅ 2026-04-10

---

### 작업 3 — `agent_review` / `get_post_detail` 응답 형식 수정

- **파일:** `routers/agent.py`
- **위치:** `agent_review` 함수 (79~97줄), `get_post_detail` 함수 (125~130줄)

#### 문제점
| 함수 | 기존 응답 | 요구 응답 |
|------|----------|----------|
| `agent_review` ok/edit | `{"post_id": ..., "status": "uploaded"}` | `{"status": "success", "message": "인스타그램 업로드가 완료되었습니다."}` |
| `agent_review` cancel | `{"post_id": ..., "status": "cancelled"}` | `{"status": "success", "message": "게시물이 취소되었습니다."}` |

#### 변경 내용
- ok/edit: `{"status": "success", "message": "인스타그램 업로드가 완료되었습니다."}`
- cancel: `{"status": "success", "message": "게시물이 취소되었습니다."}`
- `get_post_detail`은 cosmos_db 수정(작업 1)으로 이미 올바른 형식 반환 → 라우터 자체는 변경 없음

#### 완료 시각
✅ 2026-04-10

---

### 작업 4 — `send_draft_notification` 업데이트

- **파일:** `services/email_service.py`
- **함수:** `send_draft_notification`
- **위치:** 71~79줄

#### 문제점
- `shop_id` 파라미터 없음 → review 링크 생성 불가
- 제목이 `[ByBaek] 새 게시물 초안이 준비됐어요 ✂️` → featemail.md 요구 제목과 다름
- 본문에 review 링크 없이 `post_id`만 노출

#### 변경 내용
- `shop_id: str = ""` 파라미터 추가
- 제목: `"[BYBAEK] 새 인스타그램 게시물 검토 요청"`
- review 링크 구성: `{FRONTEND_URL}/review?shop_id={shop_id}&post_id={post_id}`
- 본문에 링크 포함, 캡션 미리보기 50자로 단축

#### 완료 시각
✅ 2026-04-10

---

### 작업 5 — `_send_push_notification` 버그 수정

- **파일:** `orchestrator_v2.py`
- **함수:** `_send_push_notification`
- **위치:** 595~604줄

#### 문제점
- `get_auth(shop_id)` → Auth 컨테이너 조회
- Auth 컨테이너에는 `owner_email` 없음 (Shop 컨테이너에 저장됨)
- → 이메일이 항상 `None`이어서 발송 안 됨

#### 변경 내용
- `get_onboarding(shop_id)` → Shop 컨테이너 조회로 변경
- `shop_info = onboarding.get("shop_info") or onboarding` 구조 분기 처리
- `send_draft_notification(...)` 호출 시 `shop_id=shop_id` 키워드 인자 추가
- email 없을 때 로그 출력 후 조용히 종료

#### 완료 시각
✅ 2026-04-10

---

### 작업 6 — Gmail OAuth 엔드포인트 추가

- **파일:** `routers/auth.py`
- **신규 엔드포인트:** 2개

#### 추가 내용

**`GET /api/auth/gmail?shop_id={shop_id}`**
- `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REDIRECT_URI` 환경변수 사용
- `google_auth_oauthlib.flow.Flow` 로 OAuth URL 생성
- `state=shop_id` 파라미터로 shop_id 유지
- `access_type=offline`, `prompt=consent` (refresh_token 발급용)
- Google OAuth 페이지로 302 리다이렉트

**`GET /api/auth/gmail/callback?code=...&state={shop_id}`**
- `state`에서 `shop_id` 복원
- `flow.fetch_token(code=code)` 로 토큰 교환
- `https://www.googleapis.com/oauth2/v2/userinfo` 로 이메일 조회
- `save_gmail_token(shop_id, email, access_token, refresh_token)` 호출
- 성공: `postMessage('GMAIL_LOGIN_SUCCESS', FRONTEND_URL)` + `window.close()`
- 실패: `postMessage('GMAIL_LOGIN_FAIL', FRONTEND_URL)` + 에러 메시지

#### 필요 환경변수 (`.env`에 추가 필요)
```
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REDIRECT_URI=https://{백엔드도메인}/api/auth/gmail/callback
```

#### 완료 시각
✅ 2026-04-10

---

### 작업 7 — onboarding GET 응답 형식 수정

- **파일:** `routers/onboarding.py`
- **함수:** `get_onboarding_api`
- **위치:** 201~210줄

#### 문제점
- `get_onboarding_db(shop_id)` 는 `{"shop_info": {...}}` 형태 반환
- 프론트엔드는 `data.is_gmail_connected`, `data.owner_email` 로 직접 접근
- → 항상 `undefined` 반환

#### 변경 내용
```python
shop_info = result.get("shop_info", result)
return shop_info  # 래퍼 제거, 필드 최상위 레벨 반환
```
- `result`에 `shop_info` 키가 없을 경우 `result` 자체를 반환하는 안전 처리 포함

#### 완료 시각
✅ 2026-04-10

---

### 작업 8 — `email_service.py` 환경변수 기반으로 리팩토링

- **파일:** `services/email_service.py`

#### 문제점
- `token.json` / `credentials.json` 파일에 의존
- `InstalledAppFlow.run_local_server()` — 브라우저 팝업 방식으로 Azure 서버에서 실행 불가
- Azure App Service 재시작 시 파일시스템 초기화 → 토큰 파일 소실

#### 변경 내용
- `InstalledAppFlow` import 제거
- `_TOKEN_PATH`, `_CREDS_PATH` 파일 경로 상수 제거
- `_get_gmail_service()` 를 환경변수 `GMAIL_TOKEN_JSON` 에서 JSON 파싱으로 변경:
  ```python
  token_data = json.loads(os.getenv("GMAIL_TOKEN_JSON"))
  creds = Credentials.from_authorized_user_info(token_data, SCOPES)
  ```
- access_token 만료 시 refresh_token으로 자동 갱신 유지
- 환경변수 미설정 / 토큰 완전 만료 시 명확한 에러 메시지 출력

#### Azure 환경변수 추가 필요
```
GMAIL_TOKEN_JSON = {"token":"ya29...","refresh_token":"1//...","token_uri":"https://oauth2.googleapis.com/token","client_id":"...","client_secret":"...","scopes":["https://www.googleapis.com/auth/gmail.send"]}
```

#### 로컬에서 토큰 생성하는 법
```bash
# services/ 폴더에 credentials.json 있어야 함
python -c "
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/gmail.send'])
creds = flow.run_local_server(port=0)
print(creds.to_json())
"
# 출력된 JSON 전체를 GMAIL_TOKEN_JSON 환경변수 값으로 등록
```

#### 완료 시각
✅ 2026-04-10

---

### 작업 9 — 이메일 발송 로컬 테스트 스크립트 작성

- **파일:** `services/test_email.py` (신규, 테스트 전용)

#### 목적
배포 없이 로컬에서 이메일 발송 동작을 확인하기 위한 스크립트.

#### 동작 방식
- `services/token_output.txt` 파일이 있으면 자동 로드 → `GMAIL_TOKEN_JSON` 환경변수 세팅
- 없으면 기존 환경변수 사용
- `send_draft_notification()` 직접 호출하여 실제 메일 발송 테스트

#### 사용법
```python
# test_email.py 내 수신 이메일 변경 후
TEST_TO_EMAIL = "받을이메일@example.com"
```
```powershell
cd c:/project_bybaek_web/BYBAEK-Backend
python services/test_email.py
```

#### 참고
- `token_output.txt` 는 로컬 테스트 전용 파일 — Git 커밋 금지 (`.gitignore` 추가 권장)
- `credentials_desktop.json` 도 동일하게 커밋 금지

#### 완료 시각
✅ 2026-04-10

---

## 전체 완료 요약

| 우선순위 | 작업 | 결과 |
|---------|------|------|
| 🔴 필수 | `post/detail` 응답에 `caption`, `photos` 필드 | ✅ 완료 |
| 🔴 필수 | review action별 인스타 업로드 응답 형식 | ✅ 완료 |
| 🔴 필수 | 게시물 생성 후 owner_email로 승인 이메일 발송 | ✅ 완료 (버그 수정 + 링크 포함) |
| 🟡 필요 | Gmail OAuth 구현 (사장님 이메일 수집) | ✅ 완료 |
| 🟡 필요 | onboarding 응답에 is_gmail_connected, owner_email | ✅ 완료 |
| 🔧 인프라 | email_service.py 환경변수 기반 리팩토링 | ✅ 완료 |
| 🔧 인프라 | Azure 환경변수 `GMAIL_TOKEN_JSON` 등록 | ✅ 완료 (인프라 작업) |
| ✨ 개선 | 이메일 내 승인/수정/거절 버튼 (1-click 처리) | ✅ 완료 |

## Azure 등록 환경변수 현황

| 변수명 | 용도 | 상태 |
|--------|------|------|
| `GMAIL_CLIENT_ID` | 사장님 Gmail OAuth 클라이언트 ID | ✅ 등록 완료 |
| `GMAIL_CLIENT_SECRET` | 사장님 Gmail OAuth 클라이언트 시크릿 | ✅ 등록 완료 |
| `GMAIL_REDIRECT_URI` | Gmail OAuth 콜백 URL | ✅ 등록 완료 |
| `GMAIL_TOKEN_JSON` | 앱 발신 Gmail 계정 토큰 | ✅ 등록 완료 |

## 프론트엔드 팀에 전달할 사항

> **배포 전 프론트엔드 수정 필요**

- Gmail 연결 버튼 URL 변경:
  ```typescript
  // 기존 (잘못됨)
  authUrl = 'https://accounts.google.com/';
  // 변경 후
  authUrl = `https://bybaek-b-bzhhgzh8d2gthpb3.koreacentral-01.azurewebsites.net/api/auth/gmail?shop_id=${shopId}`;
  ```
- `postMessage` 리스너 추가 — `'GMAIL_LOGIN_SUCCESS'` 수신 시 Gmail 연동 완료 UI 처리
- `postMessage` 리스너 추가 — `'GMAIL_LOGIN_FAIL'` 수신 시 에러 처리

### 작업 10 — 이메일 내 승인/수정/거절 버튼 추가

- **파일:** `services/email_service.py`, `routers/agent.py`

#### 배경
기존 이메일은 "링크를 클릭하여 프론트에서 검토하세요" 형태의 plain text였음.
사장님이 이메일에서 바로 승인/거절을 처리할 수 있도록 1-click 버튼 방식으로 개선.

#### 변경 내용

**`services/email_service.py`**

1. **HMAC 토큰 함수 추가**
   - `generate_email_token(shop_id, post_id)` — SHA-256 기반 32자 토큰 생성
   - `verify_email_token(shop_id, post_id, token)` — 위변조 방지 검증
   - 환경변수 `EMAIL_ACTION_SECRET`으로 서명 키 관리 (미설정 시 기본값 사용)

2. **HTML 이메일로 전환**
   - `_send_email_sync()` — `plain` → `html` MIME 타입 변경
   - `_build_draft_email_html()` — 3개 버튼이 포함된 HTML 이메일 빌더 추가
     - ✅ 승인 버튼 (초록): `GET /api/agent/email-action?action=approve&...`
     - ✏️ 수정 버튼 (파랑): 프론트 `/review?shop_id=...&post_id=...` 링크
     - ❌ 거절 버튼 (빨강): `GET /api/agent/email-action?action=reject&...`

3. **`send_draft_notification()` 업데이트**
   - `BACKEND_URL` 환경변수로 버튼 URL 생성
   - HMAC 토큰 포함하여 보안 강화

**`routers/agent.py`**

1. **`GET /api/agent/email-action` 엔드포인트 추가**
   - 파라미터: `action`, `shop_id`, `post_id`, `token`
   - 토큰 검증 → 실패 시 403 HTML 페이지 반환
   - `action=approve` → `_handle_upload()` 호출 → 인스타그램 바로 업로드
   - `action=reject` → `_handle_cancel()` 호출 → 게시물 취소
   - 브라우저에서 클릭하므로 JSON이 아닌 **결과 HTML 페이지** 반환

#### 이메일 버튼 동작 흐름

```
[이메일 수신]
    ↓
  ✅ 승인 클릭 → GET /api/agent/email-action?action=approve&token=xxx
                   → 토큰 검증 → _handle_upload() → 인스타 업로드 → 완료 페이지
  ✏️ 수정 클릭 → 프론트 /review 페이지로 이동 (기존 검토 페이지)
  ❌ 거절 클릭 → GET /api/agent/email-action?action=reject&token=xxx
                   → 토큰 검증 → _handle_cancel() → 취소 처리 → 완료 페이지
```

#### Azure 추가 환경변수 (선택 권장)

```
EMAIL_ACTION_SECRET=랜덤_비밀키_문자열  # 토큰 서명 키 (미등록 시 기본값 사용)
BACKEND_URL=https://bybaek-b-bzhhgzh8d2gthpb3.koreacentral-01.azurewebsites.net
```

#### 완료 시각
✅ 2026-04-10

---

## 배포 전 체크리스트

- [x] 로컬 테스트 (`services/test_email.py`) 발송 성공 확인
- [x] `credentials_desktop.json`, `token_output.txt` `.gitignore` 등록
- [ ] `feat/email` → `dev` 브랜치 PR 생성
- [ ] 프론트엔드 팀에 Gmail 버튼 URL 변경 요청 전달
- [ ] Azure 환경변수 `BACKEND_URL` 등록 확인 (이메일 버튼 URL 생성용)

---
