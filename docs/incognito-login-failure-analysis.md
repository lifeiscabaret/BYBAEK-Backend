# 시크릿창 로그인 실패 — 원인 분석

| | |
|---|---|
| **작성일** | 2026-07-18 |
| **상태** | 원인 확정(구조적) · 데모 후 수정 예정 |
| **연계 백로그** | #16 세션/인증 구조 재설계 |
| **데모 영향** | 없음 (일반 창은 정상 동작, 실제 인스타 발행 확인됨) |
| **대상** | Frontend `www.bybaekofficial.com` ↔ Backend/Easy Auth `api2.bybaekofficial.com` |

---

## TL;DR

시크릿창 로그인 실패는 **토큰 갱신 문제가 아니라 초기 로그인(OAuth) 핸드셰이크 자체의 실패**다.
Easy Auth의 OAuth `form_post` 콜백은 `login.microsoftonline.com → api2`로 가는 **크로스사이트 POST**이며, nonce 검증을 위해 **`SameSite=None`인 `Nonce` 쿠키**가 그 콜백에 실려야 한다. 시크릿창은 `SameSite=None`(서드파티) 쿠키를 기본 차단하므로 nonce 쿠키가 누락되고 → nonce 검증 실패 → **세션 쿠키(`AppServiceAuthSession`)가 발급되지 않는다.** 이후 앱은 세션이 없으니 백엔드 폴백값 `test_barber_jiyeon`(사진 0장 샵)으로 붙어 AI 업로드가 실패한다.

> **정정**: 초기 가설이었던 "MSAL 백그라운드 iframe 토큰 갱신 실패"는 아니다. 이 앱에는 MSAL이 없으며(클라이언트 라이브러리 미사용), 로그인이 **토큰 갱신 단계까지 도달하지도 못한다.** 실패는 **최초 로그인 핸드셰이크의 nonce 콜백** 한 지점이다.

---

## 아키텍처

- **로그인 방식**: `login/page.tsx`가 팝업으로 `api2.bybaekofficial.com/.auth/login/aad`(App Service Easy Auth)를 연다. 성공 시 세션 쿠키 `AppServiceAuthSession`이 **api2 도메인**에 설정된다.
- **신원 획득**: 팝업이 `MS_LOGIN_SUCCESS`를 opener(www)로 postMessage → www가 `withCredentials`로 `www → api2 /api/auth/me`를 호출해 `shop_id`를 받아 `localStorage`에 저장한다. (`login/page.tsx:47-53`)
- **백엔드 신원 판정**: `get_my_info`는 Easy Auth가 주입하는 `X-MS-CLIENT-PRINCIPAL-ID` 헤더로 신원을 판단하고, **헤더가 없으면 `test_barber_jiyeon`으로 폴백**한다. (`routers/auth.py:113-118`)

---

## 실패 메커니즘 (단계별)

```
① www 팝업 → GET  api2/.auth/login/aad
② api2   → 302   login.microsoftonline.com/.../authorize?response_mode=form_post&nonce=...
          (이때 api2가 브라우저에 Nonce 쿠키[SameSite=None] 를 심음)
③ 사용자가 Microsoft 로그인
④ MS     → POST  api2/.auth/login/aad/callback         ← login.microsoftonline.com → api2 = 크로스사이트 POST
          (Easy Auth가 ④ 요청에 실린 Nonce 쿠키로 nonce 검증)
⑤ 검증 통과 시 → AppServiceAuthSession 세션 쿠키 발급
```

- **일반 창**: 서드파티 쿠키 허용 → ④ 콜백에 `Nonce` 쿠키가 실림 → nonce 통과 → 세션 발급 → 정상.
- **시크릿 창**: 브라우저가 `SameSite=None`(서드파티) 쿠키를 **기본 차단** → ④ 콜백에서 `Nonce` 쿠키 **누락** → **nonce 검증 실패 → 로그인 핸드셰이크가 조용히 실패 → 세션 쿠키 미발급.**
- 그 결과 `www → api2 /api/auth/me`는 세션이 없어 → **`test_barber_jiyeon` 폴백** → 사진 0장 샵으로 붙어 AI 업로드 실패.

> **핵심 구분**: 두 번째 홉(`www → api2 /api/auth/me`)은 **same-site**(둘 다 `bybaekofficial.com`)이므로 시크릿창에서도 문제가 없다. 실패는 오직 **④ MS→api2 크로스사이트 콜백의 `SameSite=None` nonce 쿠키 차단** 한 지점에서 발생한다.

---

## 증거 (curl 실측)

**1) 로그인 시작 — nonce 쿠키가 `SameSite=None`, 콜백은 `form_post`:**
```
GET https://api2.bybaekofficial.com/.auth/login/aad
→ HTTP/2 302
  location: https://login.microsoftonline.com/common/oauth2/v2.0/authorize
            ?response_type=code+id_token&response_mode=form_post&nonce=...
            &client_id=18e3e886-01a6-4404-8fa1-267333a76418&...
  set-cookie: Nonce=...; path=/; secure; HttpOnly; SameSite=None        ← 핵심
  set-cookie: AppServiceSessionMode=token_or_cookie; ...; SameSite=None
```

**2) 세션 없으면 Easy Auth는 401:**
```
GET https://api2.bybaekofficial.com/.auth/me   (쿠키 없음)  → 401
```

**3) 세션 미발급 시 앱은 폴백 shop 반환:**
```
GET https://api2.bybaekofficial.com/api/auth/me   (principal 헤더 없음)
→ 200  {"shop_id":"test_barber_jiyeon","is_new":false}
```

**4) 헤더 위조 방어 확인:** 클라이언트가 `X-MS-CLIENT-PRINCIPAL-ID`를 직접 주입해도 App Service가 스트립한다(위조 헤더를 넣어도 여전히 `test_barber_jiyeon` 반환). → 신원은 **오직 Easy Auth 세션 쿠키로만** 결정된다.

---

## 확실성 표기

| 항목 | 확실성 |
|---|---|
| `Nonce` 쿠키가 `SameSite=None` | ✅ 증명 (curl `set-cookie`) |
| 로그인이 `form_post` 크로스사이트 콜백에 의존 | ✅ 증명 (curl 302 `location: response_mode=form_post`) |
| 세션 없으면 `test_barber_jiyeon` 폴백 | ✅ 증명 (curl) |
| 클라이언트 principal 헤더 스트립 | ✅ 증명 (curl) |
| 시크릿창이 콜백에서 `SameSite=None` 쿠키를 차단해 nonce 실패 | 🟡 **강한 추론** — 표준 브라우저 동작. 라이브 incognito Console/Network 직접 캡처는 미수행(자동화 브라우저가 진짜 incognito 미지원 + api2 요청 프리징) |

---

## 왜 구조적인가 (타겟 수정 불가)

근본 원인은 **frontend(`www`)와 Easy Auth/backend(`api2`)가 서로 다른 서브도메인이고, OAuth `form_post` nonce가 `SameSite=None` 서드파티 쿠키에 의존**하는 구조다. Chrome은 서드파티 쿠키를 단계적으로 폐지 중이므로, **시크릿창뿐 아니라 향후 일반 창에서도 깨질 수 있는 시한부 구조**다. 한 줄 타겟 수정으로 해결되지 않는다.

---

## #16 재설계 선택지

1. **동일 오리진 통합 (권장·가장 견고)** — 프론트와 백엔드를 같은 호스트로 묶는다(예: `www/api/*` → api2 리버스 프록시). 크로스사이트 자체가 사라져 nonce·세션 쿠키가 first-party가 된다.
2. **`response_mode=query` 전환** — form_post 대신 query 리다이렉트면 콜백이 GET이라 nonce 쿠키 의존이 줄 수 있다(Easy Auth 설정 제약 확인 필요).
3. **Bearer 토큰 인증** — Easy Auth 세션 쿠키 대신 토큰을 프론트가 보관·전송하여 쿠키 의존을 제거한다.
4. **(부수 필수) `get_my_info` 폴백 제거** — 미인증 요청은 `test_barber_jiyeon`이 아니라 **401을 반환**해야 한다. 현재 폴백은 인증 실패를 `200 + 가짜 shop`으로 감춰서 이번 혼란("로그인 된 것처럼 보이지만 실제로는 실패")의 직접 원인이다.

---

## 실제 incognito 증거 캡처 프로토콜 (2분, 문서 보강용)

위 🟡 추론을 ✅로 굳히려면 실제 시크릿창 + DevTools로:

1. 시크릿창 → DevTools → **Application → Cookies → `api2.bybaekofficial.com`** 를 보며 로그인 시도.
2. 로그인 후 **`AppServiceAuthSession` 쿠키가 생성되지 않는지** 확인(미생성 = 핸드셰이크 실패 확정).
3. **Network** 탭에서 팝업의 `/.auth/login/aad/callback` 응답과, 이후 `api/auth/me` 응답이 `{"shop_id":"test_barber_jiyeon"}` 인지 확인.
4. **Console**에 서드파티 쿠키 차단 경고(예: `Cookie "Nonce" ... blocked ... SameSite=None`)가 뜨는지 확인.

---

## 참조 코드 위치

| 역할 | 위치 |
|---|---|
| 로그인 팝업 (Easy Auth `/.auth/login/aad`) | `bybaek-frontend/src/app/login/page.tsx:96-100` |
| `MS_LOGIN_SUCCESS` → `/auth/me` → localStorage | `bybaek-frontend/src/app/login/page.tsx:47-53` |
| axios `withCredentials` 클라이언트 | `bybaek-frontend/src/api/index.ts` |
| 신원 판정 + `test_barber_jiyeon` 폴백 | `bybaek-backend/routers/auth.py:113-118` |
