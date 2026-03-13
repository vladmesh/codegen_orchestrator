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
Browser → admin-frontend (React SPA, port 3001)
              ↓ REST calls
          api-service (existing, port 8000)    ← CORS middleware needed
          worker-manager-api (new, port 8001)  ← worker introspection
              ↓
          PostgreSQL / Redis / Docker

Admin sidebar:
  Dashboard    → React (overview widgets)
  Projects     → React (hierarchy: projects → stories → tasks → runs)
  Workers      → React (list, detail, console, files, prompts)
  Queues       → React (health, contents)
  Servers      → React (resources, deploys, incidents)
  Logs         → Grafana (port 3000) — Loki logs, dashboards
  LLM Tracing  → Langfuse (port 3002) — agent call traces
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
- Встраивается в админку как отдельная страница (iframe или ссылка)

```yaml
# docker-compose.yml
langfuse:
  image: langfuse/langfuse:latest
  environment:
    DATABASE_URL: postgresql://...
    NEXTAUTH_SECRET: ...
    SALT: ...
  ports:
    - "3002:3000"
```

### Новые сервисы в docker-compose

| Сервис | Image | Port | Назначение |
|--------|-------|------|-----------|
| admin-frontend | nginx:alpine (serving built React) | 3001 | Admin SPA |
| langfuse | langfuse/langfuse:latest | 3002 | LLM tracing |
| grafana | (already exists) | 3000 | Logs & metrics |

---

## Backend: что допилить

### API-сервис (port 8000)

**CORS middleware** — React SPA на порту 3001 будет ходить в API на 8000. Нужен `CORSMiddleware` с `allow_origins=["http://localhost:3001"]`. Или проксировать через nginx (admin-frontend проксирует `/api/*` → `api:8000`). Прокси проще — без CORS вообще.

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

- Expose port 3000 на хост (сейчас только internal network)
- Anonymous viewer auth (уже настроен: `GF_AUTH_ANONYMOUS_ENABLED=true`)
- Для embed в iframe: `GF_SECURITY_ALLOW_EMBEDDING=true`

---

## Phased Plan

### Phase 1: Каркас + навигация (MVP)
- React app scaffold (Vite + shadcn/ui + TanStack Query)
- Layout: sidebar navigation, header
- Dashboard page: project count, active stories, blocked tasks, queue health, worker count
- Projects list → project detail (stories, tasks, deploys)
- Tasks list с фильтрами по status/type/story
- Task detail: events timeline, plan, current iteration
- Docker: nginx-контейнер с built React, проксирует `/api/*` → api:8000
- Grafana: port exposed, linked from sidebar

### Phase 2: Worker inspector + очереди + операции
- Worker-manager HTTP API (endpoints выше)
- Workers page: list, status, uptime, project link
- Worker detail: console (logs), prompts (CLAUDE.md + TASK.md), file tree, file viewer
- Queue health page (from `/debug/queues`)
- Action buttons: resume task, kill worker, retry failed, purge queue
- Server overview: resources, deployments, incidents

### Phase 3: Langfuse + LLM tracing
- Langfuse container в compose
- LangChain callback integration (replace LangSmith)
- Link from admin sidebar (iframe or new tab)
- Per-task trace link (task → Langfuse trace URL)

### Phase 4: Live & Visualization
- Pipeline flow (React Flow): project → stories → tasks → runs, real-time status colors
- WebSocket/SSE: worker logs live stream, queue updates
- Prometheus exporter + Grafana metrics dashboards
- API auth (task-af5bb996): admin tokens, worker scoping

---

## Action Items

### Backend
- → new task: "Worker-manager introspection API — list, logs, tree, files, prompts, kill"
- → new task: "Grafana: expose port + allow iframe embedding for admin panel"
- → backlog task-af5bb996: "API authorization" — не блокирует, но нужна до прода

### Frontend
- → new task: "Admin frontend scaffold — React + Vite + shadcn/ui + nginx container + docker-compose"
- → new task: "Admin Phase 1 — dashboard + project/task pages"
- → new task: "Admin Phase 2 — worker inspector + queues + action buttons"

### Integrations
- → new task: "Langfuse self-hosted — docker-compose + LangChain callback integration"

### Ideas (Phase 4+)
- → idea: "Pipeline flow visualization (React Flow) — real-time project pipeline"
- → idea: "WebSocket/SSE live worker log streaming"
- → idea: "Prometheus exporter — task counts, queue lengths, worker metrics"
