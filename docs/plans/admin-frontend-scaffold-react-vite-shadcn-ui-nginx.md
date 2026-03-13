# Admin frontend scaffold — React + Vite + shadcn/ui + nginx container + docker-compose

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Admin panel for the orchestrator — see brainstorm: docs/brainstorms/admin-panel.md (Phase 1).
Currently no frontend exists. API has 100+ endpoints on port 8000. Grafana already on port 3000 with anonymous viewer auth.
This task creates the React SPA scaffold with nginx proxy and docker-compose integration.

## Steps

1. [ ] Scaffold React app with Vite + TypeScript
   - **Input**: none (greenfield)
   - **Output**: `services/admin-frontend/` with Vite React 19 + TypeScript project. `package.json` with deps: react, react-dom, react-router, @tanstack/react-query, tailwindcss, lucide-react. Tailwind CSS configured. Basic `src/main.tsx` entry.
   - **Test**: `npm run build` succeeds, produces `dist/` with `index.html`

2. [ ] Install and configure shadcn/ui
   - **Input**: `services/admin-frontend/`
   - **Output**: shadcn/ui initialized (`components.json`, `src/components/ui/`). Install base components: button, card, badge, table, separator, scroll-area, sheet. Tailwind config extended with shadcn theme.
   - **Test**: `npm run build` succeeds with shadcn components importable

3. [ ] App shell — layout with sidebar and router
   - **Input**: `services/admin-frontend/src/`
   - **Output**: `src/components/layout/` — AppLayout with sidebar + header + main content area. Sidebar items: Dashboard, Projects, Tasks, Workers, Queues, Servers, Logs (external→:3000), LLM Tracing (external→:3002, disabled). React Router v7 with routes: `/`, `/projects`, `/projects/:id`, `/tasks`, `/tasks/:id`, `/workers`, `/queues`, `/servers`. Active nav state highlighting.
   - **Test**: app renders, clicking sidebar items changes route, active state shows correctly

4. [ ] API client + TanStack Query setup
   - **Input**: `services/admin-frontend/src/`
   - **Output**: `src/lib/api.ts` — thin fetch wrapper (base URL `/api`, JSON handling, error handling). `src/lib/query.ts` — QueryClient config (staleTime, retry). QueryClientProvider in App. Type definitions in `src/types/` for: Project, Story, Task, TaskEvent, QueueHealth (matching API schemas).
   - **Test**: `npm run build` succeeds, types compile

5. [ ] Dashboard page with live data
   - **Input**: API endpoints: `GET /api/projects/`, `GET /api/tasks/`, `GET /api/debug/queues`
   - **Output**: `src/pages/DashboardPage.tsx` — stat cards: total projects, active stories, tasks by status (in_dev, blocked, failed), queue health summary, timestamp. Uses TanStack Query hooks with 30s polling.
   - **Test**: page renders, shows loading state, displays data from API

6. [ ] Projects + Tasks list pages
   - **Input**: API endpoints: `GET /api/projects/`, `GET /api/tasks/?project_id=...&status=...`
   - **Output**: `src/pages/ProjectsPage.tsx` — table with name, status, story/task counts. Click → project detail. `src/pages/TasksPage.tsx` — table with title, status, type, story, priority. Status badge colors. Filter controls: status dropdown, type dropdown.
   - **Test**: pages render, navigation between them works, filters change displayed data

7. [ ] Project detail + Task detail pages
   - **Input**: API endpoints: `GET /api/projects/:id`, `GET /api/stories/?project_id=...`, `GET /api/tasks/:id`, `GET /api/tasks/:id/events`
   - **Output**: `src/pages/ProjectDetailPage.tsx` — project info + stories list + tasks grouped by story. `src/pages/TaskDetailPage.tsx` — task metadata, plan display (markdown), event timeline (chronological list with timestamps, types, descriptions).
   - **Test**: detail pages render with data, event timeline shows in order

8. [ ] Placeholder pages for Phase 2 sections
   - **Input**: router setup from step 3
   - **Output**: `src/pages/WorkersPage.tsx`, `QueuesPage.tsx`, `ServersPage.tsx` — simple placeholders with "Coming in Phase 2" message and relevant icon. Queue page can show basic `/debug/queues` data.
   - **Test**: all routes render without errors

9. [ ] Docker: nginx container + docker-compose integration
   - **Input**: `services/admin-frontend/`, `docker-compose.yml`, `Makefile`
   - **Output**: `services/admin-frontend/Dockerfile` — multi-stage (node:22-alpine build → nginx:alpine serve). `services/admin-frontend/nginx.conf` — serves SPA (try_files), proxies `/api/` → `http://api:8000/api/`. docker-compose.yml: `admin-frontend` service on port 3001, depends_on api, network internal. Makefile: include in `build` target if needed.
   - **Test**: `docker compose build admin-frontend` succeeds. `make up` starts it. `curl localhost:3001` returns HTML. `curl localhost:3001/api/health` proxies to API.

10. [ ] Grafana: enable iframe embedding
    - **Input**: `docker-compose.yml` grafana service
    - **Output**: Add `GF_SECURITY_ALLOW_EMBEDDING: "true"` to grafana env vars. Port 3000 already exposed.
    - **Test**: Grafana accessible at localhost:3000, `X-Frame-Options` header absent (allows iframe)


