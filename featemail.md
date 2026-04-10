# 백엔드 작업 명세서 — 이메일 승인 플로우

## 전체 플로우 개요

```
[AI 에이전트 게시물 생성 완료]
        ↓
[POST /api/agent/review 또는 내부 트리거]
        ↓
[사장님 owner_email로 승인 요청 이메일 발송]
        ↓
[이메일 내 링크 클릭 → 프론트엔드 /review?shop_id=XXX&post_id=YYY]
        ↓
[GET /api/agent/post/detail/{post_id}?shop_id={shop_id} 호출]
        ↓
[사장님이 캡션/사진 확인 후 "수정 및 업로드" 클릭]
        ↓
[POST /api/agent/review 호출 → 인스타그램 업로드 실행]
```

---

## 작업 1: `GET /api/agent/post/detail/{post_id}` — 응답 형식 확정

**현재 문제:** 프론트엔드가 이 엔드포인트의 응답에서 아래 필드를 직접 참조하는데, 백엔드 응답 형식이 맞지 않으면 빈 화면이 됩니다.

**프론트엔드 파싱 코드:**

```typescript
const caption = postData.caption || postData.generated_caption || '';
const postPhotos: Photo[] = (postData.photos || postData.images || []);
```

**요구 응답 형식:**

```json
{
  "post_id": "string",
  "shop_id": "string",
  "caption": "게시물 캡션 전문 (해시태그 포함)",
  "photos": [
    {
      "id": "photo_uuid",
      "original_name": "파일명.jpg",
      "blob_url": "https://...azure.blob.../파일명.jpg"
    }
  ],
  "status": "pending_review"
}
```

**중요:**

* `caption` 필드명을 우선 사용 (없으면 `generated_caption`으로도 파싱하므로 둘 다 허용됨)
* `photos` 필드명 우선 (없으면 `images`로도 파싱하므로 둘 다 허용됨)
* `photos` 배열 각 항목에 반드시 `id`, `original_name`, `blob_url` 포함

---

## 작업 2: `POST /api/agent/review` — 동작 로직 완성

**요청 형식 (프론트에서 전송):**

```json
{
  "shop_id": "string",
  "post_id": "string",
  "action": "ok" | "edit" | "cancel",
  "edited_caption": "string | null"
}
```

**action별 동작:**

| action       | 조건                                                  | 백엔드 동작                                   |
| ------------ | ----------------------------------------------------- | --------------------------------------------- |
| `"ok"`     | 캡션 수정 없이 승인                                   | 기존 캡션 그대로 인스타그램 업로드 실행       |
| `"edit"`   | 사장님이 캡션 직접 수정 후 승인                       | `edited_caption`으로 캡션 교체 후 업로드    |
| `"cancel"` | 게시물 취소 (현재 프론트에 버튼 없음, 향후 추가 가능) | 업로드 취소, 게시물 상태 `cancelled`로 변경 |

**요구 응답 형식:**

```json
{
  "status": "success",
  "message": "인스타그램 업로드가 완료되었습니다."
}
```

**중요:** 프론트가 아래와 같이 응답을 체크합니다:

```typescript
if (response.status === 'success' || response.status === 'ok' || response) {
  // 성공 처리
}
```

→ `status` 필드가 `"success"` 또는 `"ok"` 이면 됩니다. 응답 자체가 truthy이면 성공으로 처리하므로, 빈 객체 `{}` 반환도 현재는 성공으로 인식되지만, 명시적으로 `status: "success"` 포함하는 걸 권장합니다.

---

## 작업 3: 승인 요청 이메일 발송 로직

**발송 시점:** 에이전트가 게시물 생성 완료 후 (자동 스케줄 또는 수동 실행 모두)

**발송 대상:** 해당 shop의 `owner_email` (온보딩 설정에 저장된 이메일)

**이메일 내 링크 형식:**

```
https://[프론트엔드 도메인]/review?shop_id={shop_id}&post_id={post_id}
```

현재 프론트엔드 배포 URL: Azure Static Web Apps (CLAUDE.md 참고)
→ 환경변수로 `FRONTEND_URL`을 백엔드에 등록해두는 것을 권장합니다.

**이메일 본문 권장 구성:**

```
제목: [BYBAEK] 새 인스타그램 게시물 검토 요청

본문:
AI가 새 게시물을 준비했습니다.

[게시물 미리보기 - 캡션 앞 50자 + ...]

아래 버튼을 클릭하여 검토 및 수정 후 인스타그램에 업로드하세요:

[게시물 검토 및 업로드 하기] → 링크 버튼

이 링크는 1회용입니다. (선택사항 - 구현 시)
```

---

## 작업 4: Gmail OAuth 연동

> **⚠️ 프론트엔드 수정도 함께 필요합니다.** 아래 설명을 참고하여 프론트엔드에도 URL 변경 요청하세요.

### 현재 프론트 상태 (문제점)

설정 페이지 Gmail 연결 버튼 클릭 시 팝업 URL:

```typescript
authUrl = 'https://accounts.google.com/';  // ← 이건 placeholder, 실제 OAuth 아님
```

`/auth/callback` 페이지는 `code` 파라미터가 있으면 무조건 Instagram으로 처리합니다.
→ Gmail 콜백을 **별도 경로**로 처리해야 합니다.

### 권장 구현 방식

**백엔드가 Gmail OAuth 전체를 처리:**

#### 4-1. `GET /api/auth/gmail` — OAuth URL 리다이렉트 또는 반환

방법 A (리다이렉트): 요청 시 바로 Google OAuth 페이지로 리다이렉트

```
GET /api/auth/gmail
→ 302 redirect → https://accounts.google.com/o/oauth2/auth?...&redirect_uri=https://백엔드/api/auth/gmail/callback
```

방법 B (URL 반환): URL 문자열만 반환, 프론트가 팝업 오픈

```json
{ "auth_url": "https://accounts.google.com/o/oauth2/auth?..." }
```

**권장: 방법 A** — 프론트가 `/api/auth/gmail`을 팝업으로 열면, 백엔드가 바로 Google OAuth로 리다이렉트

#### 4-2. `GET /api/auth/gmail/callback` — OAuth 코드 수신 및 토큰 저장

1. Google로부터 `code` 파라미터 수신
2. `code`로 access_token + refresh_token 교환
3. Google API로 사용자 이메일 조회
4. DB에 `shop_id` 매핑 + Gmail 토큰 저장
5. `owner_email` 업데이트
6. **응답으로 HTML 페이지 반환** — 아래 내용 포함:

```html
<script>
  if (window.opener) {
    window.opener.postMessage('GMAIL_LOGIN_SUCCESS', 'https://[프론트엔드 도메인]');
    window.close();
  }
</script>
```

**중요:** `postMessage`의 두 번째 인자를 프론트엔드 origin으로 지정해야 합니다. 와일드카드 `'*'` 사용 금지 (보안).

#### 4-3. `shop_id` 전달 방법

Gmail OAuth는 stateless이므로, 어떤 shop의 Gmail 연결인지 백엔드가 알아야 합니다.

**권장:** 팝업 URL에 `state` 파라미터 포함

```
GET /api/auth/gmail?shop_id={shop_id}
→ Google OAuth URL에 state=shop_id 포함
→ 콜백에서 state 파라미터로 shop_id 복원
```

#### 4-4. 프론트엔드에 요청할 변경사항 (별도 전달)

```typescript
// 현재 (잘못됨)
authUrl = 'https://accounts.google.com/';

// 변경 후
authUrl = `https://[백엔드도메인]/api/auth/gmail?shop_id=${shopId}`;
```

---

## 작업 5: `GET /api/onboarding/{shop_id}` 응답에 Gmail 정보 포함 확인

프론트가 이미 아래 필드를 읽고 있습니다:

```typescript
setIsGmailConnected(data.is_gmail_connected || false);
setGmailAddress(data.owner_email || '');
```

→ `GET /api/onboarding/{shop_id}` 응답에 아래 필드가 이미 포함되어 있어야 합니다:

```json
{
  "is_gmail_connected": true,
  "owner_email": "owner@gmail.com",
  ...
}
```

Gmail 연동 완료 후 이 두 필드가 올바르게 저장/반환되는지 확인하세요.

---

## 전체 요약 (우선순위 순)

| 우선순위 | 작업                                                     | 관련 엔드포인트                                   |
| -------- | -------------------------------------------------------- | ------------------------------------------------- |
| 🔴 필수  | `post/detail` 응답에 `caption`, `photos` 필드 포함 | `GET /agent/post/detail/{post_id}`              |
| 🔴 필수  | review action별 인스타 업로드 로직 완성                  | `POST /agent/review`                            |
| 🔴 필수  | 게시물 생성 후 owner_email로 승인 이메일 발송            | 내부 로직                                         |
| 🟡 필요  | Gmail OAuth 구현 (팝업 → 콜백 → GMAIL_LOGIN_SUCCESS)   | `GET /auth/gmail`, `GET /auth/gmail/callback` |
| 🟡 필요  | onboarding 응답에 is_gmail_connected, owner_email 포함   | `GET /onboarding/{shop_id}`                     |

Gmail OAuth 완료 후 **프론트엔드의 팝업 URL도 `/api/auth/gmail?shop_id=XXX`로 변경**해야 합니다 (프론트 작업 필요 - 알려주세요).
