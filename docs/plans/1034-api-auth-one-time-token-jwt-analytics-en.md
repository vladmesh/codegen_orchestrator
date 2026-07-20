# #1034 ЛК API: auth (one-time token → JWT) + analytics endpoints

## Context

Task #1034 is Phase 3 of the User Dashboard (ЛК) epic. Phase 1 (infra: Promtail, Loki, container labels — #1031, #1032) and Phase 2 (analytics models, Loki client, aggregator — #1033) are all done.

**Current state**: Analytics tables (hourly, daily, known_users) exist with full CRUD API at `/api/analytics/`. PyJWT is in dependencies but unused. Redis client is streams-only. Admin auth uses `X-Telegram-ID` header — LK needs separate JWT-based auth.

**What needs to change**: Add one-time-token → JWT auth flow, add `get_lk_user()` dependency, create new `/api/lk/` router with user-facing analytics endpoints (summary, chart, status). The existing `/api/analytics/` CRUD stays as-is (internal, used by scheduler).

## Steps

1. [ ] Config + raw Redis dependency
   - **Input**: `services/api/src/config.py`, `services/api/src/dependencies.py`
   - **Output**: `LK_JWT_SECRET` field in Settings (required). New dependency `get_raw_redis()` that returns `redis.asyncio.Redis` from `RedisStreamClient.redis` property — needed for key-value ops (token storage).
   - **Test**: Unit test: Settings raises if `LK_JWT_SECRET` missing. `get_raw_redis()` raises RuntimeError if Redis not initialized.

2. [ ] LK auth: token exchange + JWT dependency
   - **Input**: `services/api/src/routers/lk_auth.py` (new), `services/api/src/dependencies.py`, `services/api/src/schemas/lk.py` (new)
   - **Output**:
     - Schemas: `TokenExchangeRequest(token: str)`, `TokenExchangeResponse(access_token: str, token_type: str = "bearer")`
     - `POST /api/lk/auth/token`: validate token against Redis key `lk_token:{token}`, delete key (one-time), look up User by id, return JWT with `sub=user_id`, `exp=24h`. Raises 401 if token expired/missing.
     - `get_lk_user()` dependency: decode `Authorization: Bearer <jwt>` header, extract `sub`, look up User by id. Raises 401 if invalid/expired.
     - JWT secret from `settings.LK_JWT_SECRET`, algorithm HS256.
   - **Test**: Service test: set Redis key `lk_token:test-uuid → user_id` → POST token exchange → get JWT → decode and verify claims. Test expired/used token → 401. Test `get_lk_user` with valid JWT → returns user, with garbage → 401.

3. [ ] LK router: project list with latest summary
   - **Input**: `services/api/src/routers/lk.py` (new), schemas from step 2
   - **Output**:
     - Schema: `LkProjectSummary(id, name, status, latest_daily: AnalyticsDailyRead | None)`
     - `GET /api/lk/projects` — `Depends(get_lk_user)`, query projects where `owner_id = user.id`, LEFT JOIN latest `analytics_daily` per project. Returns list of `LkProjectSummary`.
   - **Test**: Service test: create user + 2 projects (one with daily row, one without) → verify list returns both, one with summary, one with null.

4. [ ] LK router: project summary endpoint
   - **Input**: `services/api/src/routers/lk.py`, analytics models
   - **Output**:
     - Schema: `ProjectSummaryResponse(total_users, new_users, dau, wau, returning_pct, total_requests, error_rate, p95_ms, top_endpoints, breakdown: list[ServiceBreakdown])`
     - `GET /api/lk/projects/{id}/summary?period=24h|7d|30d`:
       - Verify project ownership (owner_id == user.id, else 403)
       - period=24h → aggregate from `analytics_hourly` (last 24h)
       - period=7d/30d → aggregate from `analytics_daily` (last 7/30 days)
       - WAU: `COUNT(DISTINCT user_id_hash)` from hourly over 7 days
       - Breakdown per `service_name`
   - **Test**: Service test: insert hourly/daily rows for a project → GET summary for each period → verify aggregation math (totals, rates, percentiles).

5. [ ] LK router: chart endpoint
   - **Input**: `services/api/src/routers/lk.py`
   - **Output**:
     - Schema: `ChartDataPoint(date: str, value: float)`, `ChartResponse(metric, period, data: list[ChartDataPoint])`
     - `GET /api/lk/projects/{id}/chart?metric=users|requests|errors&period=7d`:
       - Ownership check
       - metric=users → unique_users from daily
       - metric=requests → total_requests from daily
       - metric=errors → error_rate from daily
       - Returns time series ordered by date
   - **Test**: Service test: insert 7 daily rows → GET chart for each metric → verify data points match inserted values.

6. [ ] LK router: status endpoint
   - **Input**: `services/api/src/routers/lk.py`, analytics models
   - **Output**:
     - Schema: `ServiceStatus(name: str, status: str, last_seen: datetime | None)`, `ProjectStatusResponse(services: list[ServiceStatus])`
     - `GET /api/lk/projects/{id}/status`:
       - Ownership check
       - Query latest `analytics_hourly` per `service_name` for this project
       - status = "up" if last bucket < 2h ago, else "down"
       - last_seen = latest bucket timestamp
   - **Test**: Service test: insert recent hourly row (status=up) and old one (status=down) → verify response.

7. [ ] Register routers + integration test
   - **Input**: `services/api/src/main.py`, `services/api/src/routers/__init__.py`
   - **Output**: Both `lk_auth` and `lk` routers registered with `/api` prefix. Full integration test: create user → set Redis token → exchange for JWT → call all 4 LK endpoints → verify responses. Add `LK_JWT_SECRET` to `.env.example` and docker-compose env.
   - **Test**: Integration test covering the complete auth → analytics flow end-to-end.

