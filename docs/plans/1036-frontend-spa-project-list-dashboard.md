# #1036 ЛК frontend SPA (project list + dashboard)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Final task in the User Dashboard story. Backend is fully built:
- #1031–#1032: Promtail + container labels → logs flow to Loki per project
- #1033: Analytics aggregation (hourly/daily models, Loki client, scheduler job)
- #1034: LK API — JWT auth (`POST /api/lk/auth/token`), 4 analytics endpoints (`/projects`, `/projects/{id}/summary`, `/projects/{id}/chart`, `/projects/{id}/status`)
- #1035: Telegram bot `/dashboard` command — generates one-time Redis token, sends URL button

This task builds the frontend SPA that the user lands on after tapping the Telegram button. Pattern: same as `services/admin-frontend/` (Vite + React + Tailwind, nginx in Docker), but **no Basic Auth** — auth is JWT-based via the token exchange flow.

## Steps

1. [ ] Scaffold project + Docker + docker-compose
   - **Input**: `services/admin-frontend/` (reference pattern), `docker-compose.yml`, `infra/Caddyfile`
   - **Output**:
     - `services/user-dashboard/` — Vite + React + TypeScript + Tailwind project (`npm create vite`, copy config patterns from admin-frontend)
     - `package.json` with deps: react, react-dom, react-router, recharts, tailwindcss, @tailwindcss/vite, clsx, tailwind-merge, lucide-react
     - `Dockerfile` (node build → nginx, no htpasswd — JWT auth handled client-side)
     - `nginx.conf` — serve SPA, proxy `/api/` to `http://api:8000/api/`, SPA fallback for all routes
     - `entrypoint.sh` — minimal (just `exec nginx -g 'daemon off;'`)
     - `docker-compose.yml` entry: `user-dashboard` service, port 3002, depends on api
   - **Test**: `docker compose build user-dashboard` succeeds; container starts and serves index.html

2. [ ] Auth page + API client + JWT storage
   - **Input**: LK auth API (`POST /api/lk/auth/token`), schemas from `services/api/src/schemas/lk.py`
   - **Output**:
     - `src/lib/api.ts` — fetch wrapper that injects `Authorization: Bearer <jwt>` from localStorage, redirects to `/auth` on 401
     - `src/pages/AuthPage.tsx` — reads `?token=` from URL, calls `POST /api/lk/auth/token`, stores JWT in localStorage, redirects to `/projects`. Shows spinner during exchange, error message on failure.
     - `src/lib/auth.ts` — `getToken()`, `setToken()`, `clearToken()`, `isAuthenticated()` helpers
     - `src/components/ProtectedRoute.tsx` — wraps routes, redirects to `/auth` if no JWT
     - `src/App.tsx` with routes: `/auth` → AuthPage, `/projects` → ProjectsPage (stub), `/projects/:id` → DashboardPage (stub)
   - **Test**: Build succeeds, auth flow works manually (token exchange → JWT stored → redirect)

3. [ ] Project list page
   - **Input**: `GET /api/lk/projects` response shape (LkProject + LatestDailySummary)
   - **Output**:
     - `src/pages/ProjectsPage.tsx` — fetches projects, renders cards with: project name, status badge (active/draft), key metrics from latest_daily (users, requests/day, error rate, p95). Click navigates to `/projects/:id`. Empty state: "Нет данных о проектах".
     - `src/components/ProjectCard.tsx` — single project card component
     - `src/components/StatusBadge.tsx` — colored status indicator
     - `src/components/MetricValue.tsx` — formatted metric display (e.g., "1.2k", "45ms", "0.3%")
     - User-friendly labels: "Пользователи", "Запросы/день", "Скорость", "Ошибки"
   - **Test**: Build succeeds, page renders with mock data

4. [ ] Project dashboard page — KPI cards + period selector
   - **Input**: `GET /api/lk/projects/{id}/summary` response (ProjectSummaryResponse), `GET /api/lk/projects/{id}/status` (ProjectStatusResponse)
   - **Output**:
     - `src/pages/DashboardPage.tsx` — period selector (24ч / 7д / 30д), 4 KPI cards, service status section, top endpoints, per-service breakdown
     - `src/components/KpiCard.tsx` — large number + label + optional delta indicator
     - `src/components/PeriodSelector.tsx` — 3 buttons (24h/7d/30d), current highlighted
     - `src/components/ServiceStatusList.tsx` — per-service up/down indicators
     - `src/components/TopEndpoints.tsx` — ranked list of top 5 endpoints
     - `src/components/ServiceBreakdown.tsx` — table with per-service metrics
     - KPI cards: "Пользователи" (total_users), "Запросы" (total_requests), "Ошибки" (error_rate as %), "Скорость" (p95_ms as "Xms")
   - **Test**: Build succeeds, page renders with summary data

5. [ ] Chart component
   - **Input**: `GET /api/lk/projects/{id}/chart` response (ChartResponse), recharts library
   - **Output**:
     - `src/components/MetricChart.tsx` — line chart (Recharts) with metric switcher (users/requests/errors). Fetches data via `/chart?metric=X&period=Y`. Responsive, mobile-friendly.
     - Integrate into DashboardPage below the KPI cards
   - **Test**: Build succeeds, chart renders with data points

6. [ ] Polish: layout, navigation, mobile, empty states
   - **Input**: All pages from steps 2–5
   - **Output**:
     - `src/components/Layout.tsx` — minimal header with project name (on dashboard), back button, logo
     - `src/components/Spinner.tsx` — loading indicator
     - `src/components/ErrorMessage.tsx` — error display
     - Mobile-responsive: cards stack vertically, chart fills width, period selector scrollable
     - Empty states: no data yet → friendly message, not blank screen
     - Color scheme: clean neutrals, green for "up"/good, red for errors
   - **Test**: `docker compose build user-dashboard` succeeds; manual check on mobile viewport

