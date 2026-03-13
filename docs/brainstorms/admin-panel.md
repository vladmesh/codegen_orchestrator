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

### Phase 1.5: Auth + единая точка входа — #task-d87d08bf

Проблема: Grafana (:3000) и API (:8000) торчат наружу без авторизации. Всё открывается в отдельных вкладках.

Решение — **один порт, один логин, всё внутри**:
- **Авторизация** на уровне nginx (basic auth или session cookie). Один раз ввёл логин/пароль — доступ ко всему за проксёй.
- **Grafana проксируется** через admin-frontend nginx: `/grafana/*` → `grafana:3000`. В sidebar — iframe, не новая вкладка.
- **Grafana sub-path**: `GF_SERVER_ROOT_URL`, `GF_SERVER_SERVE_FROM_SUB_PATH=true`.
- **Закрыть лишние порты**: убрать `ports:` у Grafana (3000) из docker-compose. Снаружи доступен только :3001.
- Потенциально: Langfuse тоже через прокси (`/langfuse/*`), когда будет задеплоен.

### Phase 2: Worker inspector + очереди + операции
- Worker-manager HTTP API (endpoints выше)
- Workers page: list, status, uptime, project link
- Worker detail: console (logs), prompts (CLAUDE.md + TASK.md), file tree, file viewer
- Queue health page (from `/debug/queues`)
- Action buttons: resume task, kill worker, retry failed, purge queue
- Server overview: resources, deployments, incidents

### Phase 3: Langfuse + LLM tracing
- Langfuse container в compose (без внешнего порта — через admin-frontend proxy)
- LangChain callback integration (replace LangSmith)
- `/langfuse/*` proxy в nginx, iframe в SPA
- Per-task trace link (task → Langfuse trace URL)

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

### Backend
- → new task: "Worker-manager introspection API — list, logs, tree, files, prompts, kill"
- → backlog task-af5bb996: "API authorization" — не блокирует, но нужна до прода

### Frontend
- → **next**: #task-d87d08bf "Admin auth + single entry point — proxy Grafana, close extra ports"
- → new task: "Admin Phase 2 — worker inspector + queues + action buttons"

### Integrations
- → new task: "Langfuse self-hosted — docker-compose + LangChain callback integration"

### Ideas (Phase 4+)
- → idea: "Pipeline flow visualization (React Flow) — real-time project pipeline"
- → idea: "WebSocket/SSE live worker log streaming"
- → idea: "Prometheus exporter — task counts, queue lengths, worker metrics"
