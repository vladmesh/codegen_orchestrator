# Админка — архитектура и план

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

# Brainstorm: Админка — архитектура и план

> **Дата**: 2026-03-13
> **Контекст**: Нужна админка для мониторинга и управления оркестратором — проекты, таски, воркеры, очереди, логи, трейсинг агентов, инспекция worker-контейнеров
> **Status**: done

---

## Current State

### Что уже есть
- **REST API**: 20 роутеров, 100+ эндпоинтов. Все сущности (projects, stories, tasks, runs, servers, incidents, queues) уже queryable.
- **Логи**: Loki + Promtail + Grafana. JSON-логи всех сервисов + worker-контейнеров (фильтр по `com.codegen.type=worker`). Correlation ID для трейсинга запросов.
- **Debug endpoint**: `GET /debug/queues` — здоровье Redis streams (длина, pending, consumer groups).
- **Task events**: полный аудит-трейл по каждой таске (transitions, notes, CI fixes, deviations).
- **Server monitoring**: `last_health_check`, CPU/RAM/disk used, incidents с recovery.
- **Worker metadata в Redis**: `worker:status:{id}`, `worker:meta:{id}` (workspace_path, project_id, dev_network), `worker:error:{id}`, `worker:last_activity:{id}`.
- **Worker labels**: контейнеры создаются с `com.codegen.type=worker` и `com.codegen.worker.id={id}`.
- **Worker workspace на хосте**: `/tmp/codegen/workspaces/{project_id}/workspace/` или `/data/workspaces/{repo_id}/` — полный git-репо, читаем с хоста.
- **Worker instructions**: `CLAUDE.md` (generic) + `TASK.md` (task-specific: project spec, описание, modules) — пишутся wrapper'ом в workspace перед запуском агента.
- **SpawnResult**: stdout, commit_sha, REPORT.md, exit_code, block/reject reasons — возвращается после завершения воркера.

### Что нужно
Единый интерфейс где видно:
1. Проекты → стори → таски → раны (иерархия со статусами)
2. Живые воркеры (контейнеры, что делают, логи, промпты, файлы)
3. Очереди (что в них, длина, застрявшие)
4. Логи сервисов (поиск, фильтрация, correlation)
5. Серверы (ресурсы, деплойменты, инциденты)
6. Трейсинг LLM-вызовов агентов (промпты, ответы, tool calls, стоимость)

---

## Решение (после обсуждения)

**React SPA + Grafana (логи/метрики) + Langfuse (LLM tracing)**

### Почему React, не HTMX:
- Pipeline visualization, real-time updates, drag-and-drop — потребуются
- HTMX ограничен: нет client-side state, каждое действие = round-trip, нет экосистемы компонентов (таблицы с сортировкой, графы, charts)
- Чтобы не переписывать позже, сразу полноценный фронт

### Архитектура

```
Browser → admin-frontend (React SPA, port 3001) ← единственный внешний порт
              ↓ nginx reverse proxy (с авторизацией)
          /api/*     → api-service (port 8000, internal)
          /grafana/* → grafana (port 3000, internal)
          /langfuse/* → langfuse (port 3002, internal)  [Phase 3]
          /wm-api/*  → worker-manager (port 8001, internal)  [Phase 2]
              ↓
          PostgreSQL / Redis / Docker

Admin sidebar:
  Dashboard    → React (overview widgets)
  Projects     → React (hierarchy: projects → stories → tasks → runs)
  Workers      → React (list, detail, console, files, prompts)
  Queues       → React (health, contents)
  Servers      → React (resources, deploys, incidents)
  Logs         → Grafana (embedded iframe через /grafana/)
  LLM Tracing  → Langfuse (embedded iframe через /langfuse/)
```

### Стек фронтенда

| Слой | Технология | Почему |
|------|-----------|--------|
| Framework | React 19 + TypeScript | Стандарт, экосистема |
| Build | Vite | Быстрый dev-сервер |
| UI Kit | shadcn/ui + Tailwind | Копируемые компоненты, не зависимость |
| Таблицы | TanStack Table | Фильтры, сортировка, пагинация |
| Графы | React Flow | Pipeline visualization (node/edge) |
| Charts | Recharts | Простые графики (burndown, queue health) |
| State | TanStack Query | Server state, кеширование, polling |
| Router | React Router v7 | SPA routing |

### Langfuse

Self-hosted, Docker-контейнер. Open-source аналог LangSmith.
- Трейсинг каждого LLM-вызова: промпт → ответ → tool calls → latency → tokens → cost
- Для LangGraph — полное дерево выполнения графа
- Интеграция через LangChain callback (заменяет `LANGCHAIN_TRACING_V2`)
- UI для просмотра трейсов, prompt playground, dataset management
- Встраивается в админку через nginx proxy + iframe (не отдельная вкладка)

```yaml
# docker-compose.yml
langfuse:
  image: langfuse/langfuse:latest
  environment:
    DATABASE_URL: postgresql://...
    NEXTAUTH_SECRET: ...
    SALT: ...
  # НЕ экспозим порт наружу — только через admin-frontend nginx
  networks:
    - internal
```

### Новые сервисы в docker-compose

| Сервис | Image | Ext Port | Назначение |
|--------|-------|----------|-----------|
| admin-frontend | nginx:alpine (serving built React) | 3001 | Admin SPA + reverse proxy (единственная точка входа) |
| langfuse | langfuse/langfuse:latest | — | LLM tracing (проксируется через admin-frontend) |
| grafana | (already exists) | — | Logs & metrics (проксируется через admin-frontend) |

---

## Backend: что допилить

### API-сервис (port 8000)

**CORS** — не нужен. Nginx в admin-frontend проксирует `/api/*` → `api:8000`. ✅ Решено.

**API auth (существующая задача task-af5bb996)** — не блокирует Phase 1, но нужна до прода:
- Project-scoped токены для воркеров (чтобы не шалили)
- Admin-only на деструктивные endpoints (DELETE project/task/story)
- Service-level токены для внутренних сервисов
- Админка ходит с admin-токеном

### Worker-manager introspection API (новый, port 8001)

Worker-manager — единственный сервис с Docker socket и доступом к workspace volumes. Нужен минимальный HTTP API для инспекции воркеров:

| Endpoint | Что делает | Источник данных |
|----------|-----------|----------------|
| `GET /workers/` | Список активных воркеров | Redis `worker:status:*` + `worker:meta:*` |
| `GET /workers/{id}` | Детали: status, project, workspace, uptime | Redis metadata |
| `GET /workers/{id}/logs` | Последние N строк stdout | `docker.containers.get().logs()` |
| `GET /workers/{id}/logs?follow=true` | SSE/WebSocket live tail | `docker.containers.get().logs(stream=True)` |
| `GET /workers/{id}/tree` | Список файлов workspace | `os.walk(workspace_path)` |
| `GET /workers/{id}/files/{path}` | Содержимое файла | `open(workspace_path / path)` |
| `GET /workers/{id}/prompts` | CLAUDE.md + TASK.md | Чтение из workspace |
| `GET /workers/{id}/result` | SpawnResult (output, report, commit) | Redis / in-memory |
| `DELETE /workers/{id}` | Kill воркера | `docker.containers.get().remove(force=True)` |

Реализация: FastAPI app внутри worker-manager, слушает на `0.0.0.0:8001` (internal network only). Лёгкий — 200 строк максимум.

**Безопасность workspace browsing**: ограничить чтение только внутри workspace_path (path traversal protection). Нормализовать путь, проверить что не выходит за пределы.

### Grafana

- ~~Expose port 3000 на хост~~ → порт НЕ экспозим, проксируем через admin-frontend
- Anonymous viewer auth (уже настроен: `GF_AUTH_ANONYMOUS_ENABLED=true`)
- Для embed в iframe: `GF_SECURITY_ALLOW_EMBEDDING=true` ✅ Сделано
- Sub-path: `GF_SERVER_ROOT_URL=http://localhost:3001/grafana/`, `GF_SERVER_SERVE_FROM_SUB_PATH=true`

---

## Phased Plan

### Phase 1: Каркас + навигация (MVP) ✅ DONE — #task-57cc3462

- ✅ React app scaffold (Vite + shadcn/ui + TanStack Query)
- ✅ Layout: sidebar navigation, header
- ✅ Dashboard page: project count, tasks by status, queue health
- ✅ Projects list → project detail (stories, tasks)
- ✅ Tasks list с фильтрами по status/type + task detail с events timeline
- ✅ Docker: nginx-контейнер с built React, проксирует `/api/*` → api:8000 + `/debug/*`
- ✅ Grafana: `GF_SECURITY_ALLOW_EMBEDDING=true`, ссылка на дашборд в sidebar
- ✅ External links используют `window.location.hostname` (не hardcoded localhost)

### Phase 1.5: Auth + единая точка входа ✅ DONE — #task-d87d08bf

- ✅ Nginx basic auth (htpasswd from ADMIN_USER/ADMIN_PASSWORD env vars, generated at container start)
- ✅ Grafana proxied through `/grafana/` (sub-path config: GF_SERVER_ROOT_URL + GF_SERVER_SERVE_FROM_SUB_PATH)
- ✅ Logs page: iframe embedding Grafana dashboard (not external tab)
- ✅ Grafana port 3000 closed (internal only, proxied through admin-frontend)
- ✅ API port 8000 kept for local dev scripts (make sync, generate_backlog.py)
- ✅ /health excluded from auth (Docker healthcheck)
- ✅ Sidebar simplified: all routes internal, no external link logic

### Phase 1.5b: Observability fixes (hotfix после Phase 1.5)

- ✅ Promtail: добавлен explicit `job=docker` label (docker_sd_configs не ставит автоматически)
- ✅ Dashboard: stream selector переключён на `compose_service` (вместо `job="docker"`)
- ✅ Dashboard: `allValue` исправлен `.*` → `.+` (Loki отвергает empty-compatible regex)
- ✅ Dashboard: level filter переключён на `label_values(level)` (auto-populate из Loki)
- ✅ Unified JSON logging: Loki (`-log.format=json`), Grafana (`GF_LOG_CONSOLE_FORMAT=json`), Promtail (`-log.format=json`), Caddy (`log { format json }`), nginx (custom `json_log` format)
- ✅ Promtail pipeline упрощён до одного `json` stage (все сервисы теперь JSON)
- ✅ Normalize `warn` → `warning` (Go vs Python)
- ✅ `grafana-lokiexplore-app` плагин отключён (ломается с sub-path proxy, не критичен)
- ✅ `GF_SERVER_ROOT_URL` указывает на внешний hostname (для корректного WebSocket URL)
- ⚠️ Redis и PostgreSQL остаются plain text (нет JSON-режима / несовместим с Docker stdout)

### Phase 2: Worker inspector + очереди + операции ✅ DONE — #task-6d8257e5
- ✅ Worker-manager HTTP API (7 endpoints: list, detail, logs, tree, files, prompts, kill)
- ✅ Workers page: list, status (RUNNING/GONE/DEAD), project link, auto-refresh 5s
- ✅ Worker detail: console (logs + tail selector), prompts (CLAUDE.md + TASK.md), file tree + file viewer
- ✅ Queue health page (from `/debug/queues` — proper DebugQueuesResponse types, issues banner)
- ✅ Action buttons: resume task (WHR → in_dev with guidance), retry failed (→ backlog), kill worker
- ✅ Queue message browser: click queue → messages list + pending tab, ack/delete actions
- Server overview: resources, deployments, incidents (deferred)

### Phase 3: Langfuse + LLM tracing — PARTIALLY DONE

**Решения (обсуждение 2026-03-13):**
- **Версия**: Langfuse v3 (текущая, v2 deprecated)
- **ClickHouse**: добавляем отдельный контейнер (обязателен для v3 — хранит traces/observations)
- **PostgreSQL**: отдельный database `langfuse` на том же `db` контейнере (Langfuse использует Prisma, конфликтует с Alembic в shared schema). Init script `CREATE DATABASE langfuse`.
- **Redis**: шарим существующий (без auth)
- **S3/MinIO**: ~~не нужен~~ → **обязателен для v3** (event/media storage не опционален, без S3 env vars Langfuse падает с ZodError)
- **Интеграция**: env-var drop-in для старта (`LANGFUSE_HOST` + keys + `LANGCHAIN_TRACING_V2=true` — zero code changes). Explicit callbacks (per-task metadata) — позже.
- **Nginx proxy**: ❌ **отказались от iframe** — Next.js hardcodes absolute paths (`/_next/`, `/api/auth/`, `/api/trpc/`), `NEXT_PUBLIC_BASE_PATH` — build-time переменная, prebuilt образ не поддерживает. `sub_filter` в nginx не помогает — webpack runtime генерит пути в JS, не в HTML.
- **Текущее решение**: кнопка "Open Langfuse" ведёт на `hostname:3002` (прямой порт). Langfuse UI — отдельная вкладка.
- **Trace → Task linking**: отдельная задача после настройки Langfuse

**Что сделано:**
1. ✅ Langfuse infra — docker-compose (langfuse-web + langfuse-worker + ClickHouse + MinIO), env vars, init script, auto-provisioning — #task-a51fb1cf
2. ✅ LangChain callback integration — env-var drop-in, трейсы идут — #task-300f55e6
3. ✅ Admin SPA — страница LLM Tracing (внешняя ссылка, не iframe) + Users entity + owner links — #task-df069084
4. ✅ Users entity в админке: `/users` (список), `/users/:id` (детали + проекты), owner link на странице проекта

**Что НЕ сделано — требует отдельного спринта (Phase 3b):**

⚠️ **Настройка Langfuse** (без этого трейсы бесполезны):
- **Прописать цены моделей**: OpenRouter передаёт имена типа `anthropic/claude-sonnet-4-5` — Langfuse их не знает, стоимость = $0.00. Нужно: Settings → Models → добавить все используемые модели с ценами (input/output per 1M tokens)
- ✅ ~~**Разбивка по пользователям**~~: `langfuse_user_id` передаётся во всех consumers через `build_langfuse_metadata()`
- ✅ ~~**Разбивка по проектам**~~: `langfuse_session_id` = project_id + тег `project:{id}` — группировка и фильтрация в Langfuse UI
- ✅ ~~**Разбивка по нодам (агентам)**~~: тег `agent:{type}` (`po`, `architect`, `engineering`, `deploy`) во всех consumers
- ✅ **Task/Story ID в metadata**: `task_id` и `story_id` передаются как custom metadata (architect, engineering, deploy)
- **Trace → Task linking**: передавать `task_id` в metadata ✅, сохранять `trace_id` в БД и показывать ссылку на трейс на странице таска в админке — NOT DONE

**Langfuse → Admin интеграция (данные в своей админке):**
- ✅ Nginx proxy `/langfuse-api/` → Langfuse public API (auth header injected by entrypoint, ключи не на фронте)
- ✅ Dashboard: виджет "Recent LLM Traces" — последние 10 трейсов с agent type badge, user, latency
- ✅ User detail: таб "Messages" — полная переписка юзера с ПО (сообщения, tool calls, tool results, системные events), auto-scroll к последнему сообщению

**Уроки Phase 3:**
- MinIO обязателен (S3 env vars валидируются Zod'ом при старте — без них ZodError)
- `LANGFUSE_INIT_*` env vars: если хотя бы один пустой — Langfuse падает. Либо все заполнены, либо ни один не передан.
- `NEXT_PUBLIC_BASE_PATH` — build-time переменная, prebuilt образ её не поддерживает. Iframe embedding Next.js за sub-path без пересборки — невозможно.
- `sub_filter` в nginx бесполезен для JS-приложений: HTML перезаписывается, но webpack runtime (`__webpack_require__.p`) хардкодит пути в JS-бандле.
- Langfuse v3 auth (`/api/auth/session`) конфликтует с нашим `/api/` proxy — даже если проксировать, нужно разруливать auth-потоки Next.js отдельно.

### Phase 4: Live & Visualization
- Pipeline flow (React Flow): project → stories → tasks → runs, real-time status colors
- WebSocket/SSE: worker logs live stream, queue updates
- Prometheus exporter + Grafana metrics dashboards
- API auth (task-af5bb996): admin tokens, worker scoping

---

## Action Items

### Done
- ✅ ~~"Admin frontend scaffold — React + Vite + shadcn/ui + nginx container + docker-compose"~~ → #task-57cc3462
- ✅ ~~"Grafana: expose port + allow iframe embedding for admin panel"~~ → сделано в рамках #task-57cc3462
- ✅ ~~CORS middleware~~ → не нужен, решено nginx proxy
- ✅ ~~Observability fixes: unified JSON logging + Grafana dashboard~~ → hotfix после Phase 1.5

### Backend
- → new task: "Worker-manager introspection API — list, logs, tree, files, prompts, kill"
- → backlog task-af5bb996: "API authorization" — не блокирует, но нужна до прода

### Frontend
- ✅ ~~"Admin auth + single entry point — proxy Grafana, close extra ports"~~ → #task-d87d08bf
- → new task: "Admin Phase 2 — worker inspector + queues + action buttons"

### Integrations (Phase 3)
- ✅ ~~#task-a51fb1cf: "Langfuse v3 infra — docker-compose + ClickHouse + MinIO + nginx proxy"~~ → done
- ✅ ~~#task-300f55e6: "LangChain → Langfuse tracing integration (env-var drop-in)"~~ → done
- ✅ ~~#task-df069084: "Admin SPA — LLM Tracing page + Users entity"~~ → done (внешняя ссылка, не iframe)
- → new sprint: "Langfuse настройка — цены моделей, разбивка по users/projects/agents, trace-task linking"

### Workspaces in admin
- → new task: "Admin workspace browser — project-level workspace view with file tree"
  - Воркспейсы принадлежат проектам (не воркерам). Один воркспейс переиспользуется между воркерами одного проекта.
  - Показывать воркспейс: на странице проекта `/projects/:id` (вкладка "Workspace") — дерево файлов + просмотр содержимого
  - На странице воркера — ссылка на workspace того же проекта (если воркспейс жив на диске)
  - Worker-manager introspect API уже имеет `/tree` и `/files/{path}` — нужен аналог привязанный к project_id (не worker_id)
  - Нужен новый эндпоинт: `GET /api/introspect/workspaces/{project_id}/tree` и `GET /api/introspect/workspaces/{project_id}/files/{path}`
  - Данные: `workspace:active_projects` set + workspace_path из worker:meta или прямой путь `WORKSPACE_BASE_PATH/{project_id}/workspace/`

### Ideas (Phase 4+)
- → idea: "Pipeline flow visualization (React Flow) — real-time project pipeline"
- → idea: "WebSocket/SSE live worker log streaming"
- → idea: "Prometheus exporter — task counts, queue lengths, worker metrics"

