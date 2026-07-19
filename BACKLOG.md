# BYBAEK Backend — 백로그

## 보안 (SECURITY)

### [ ] `/api/agent/*` 인증 부재 — 데모 후 실제 인증 추가 (등록일 2026-07-16)

**배경**
- 프론트(`www.bybaekofficial.com`) → 백엔드(`api2.bybaekofficial.com`) `POST /api/agent/run` 호출 시 403 발생.
- 원인: App Service **Easy Auth(authV2)** 의 `globalValidation.requireAuthentication=true` 가 FastAPI 도달 전에 요청을 차단. (`X-Ms-Middleware-Request-Id` 존재 / 본문 없음 / CORS 헤더 없음)
- `routers/agent.py` 의 라우트에는 `Depends(...)` 기반 인증이 **전혀 없음** (`AgentRunRequest` 에도 토큰 필드 없이 `shop_id` 만 존재). 즉 agent 라우터는 그동안 Easy Auth 플랫폼 게이트에만 의존.

**임시 조치 (2026-07-16, 데모용)**
- Azure App Service `bybaek-backend` 의 Easy Auth `excludedPaths` 에 `/api/agent` 추가 → 해당 경로만 익명 허용.
- `az webapp auth update --name bybaek-backend --resource-group rg-bybaek --excluded-paths "/api/agent"`
- **부작용/위험:** 현재 `/api/agent/*` 는 `shop_id` 만 알면 **누구나 호출 가능**. (게시물 조회/생성/저장/업로드 트리거 노출)
- CORS 는 변경하지 않음 — FastAPI `CORSMiddleware` 가 `FRONTEND_URL` env(`https://www.bybaekofficial.com`)로 이미 정상 처리. App Service CORS 는 건드리지 않음(중복/충돌 방지).

**데모 후 해야 할 일 (TODO)**
- [ ] `/api/agent/*` 에 실제 인증 dependency 추가. 예: 프론트가 넘기는 세션 토큰(또는 Easy Auth `X-MS-CLIENT-PRINCIPAL`)을 FastAPI `Depends()` 로 검증.
- [ ] `shop_id` 소유권 검증 (요청자가 해당 shop 의 소유자인지 확인 → 타 shop 데이터 접근 차단).
- [ ] 인증 추가 후 Easy Auth `excludedPaths` 에서 `/api/agent` 제거 여부 재검토(FastAPI 자체 인증으로 대체 시).
