# Glossary / Словарь терминов

Единая терминология для проекта codegen_orchestrator.

## Core Concepts

### Service (Сервис)
Долгоживущий процесс. Один контейнер = один сервис.

**Примеры:** `api`, `telegram-bot`, `langgraph`, `scheduler`

### Consumer (Консьюмер)
**Роль, а не имя сервиса.** Любой сервис или компонент, который слушает Redis queue.

Сервис становится consumer'ом только в контексте конкретной очереди:
- `langgraph` — consumer для `engineering:queue`, `deploy:queue`
- `infra-service` — consumer для `provisioner:queue` (provisioning only)
- `worker-manager` — consumer для `worker:commands`
- `worker-wrapper` — consumer для `worker:*:input` (внутри контейнера воркера)

> **Важно:** Не путайте с именем сервиса. Нет сервиса `engineering-consumer` — есть сервис `langgraph`, который является consumer'ом очереди `engineering:queue`.

### Worker (Воркер)
Docker-контейнер с CLI coding agent внутри. Используется только для Developer workers.

| Type | Lifecycle | Queue Pattern | Session |
|------|-----------|---------------|---------|
| **Developer Worker** | Per-story (reused) or per-task (standalone) | `worker:{worker_id}:*` | No (stateless) |

**Developer Worker** — Контейнер с coding agent. Для задач внутри Story — переиспользуется между задачами (worker_id хранится в Redis hash `story:workers`). Для standalone задач — эфемерный, удаляется после завершения. Stateless — контекст это код в репо + ошибки.

**Управляется:** `worker-manager`
**Конфигурация:** Промпты хранятся в `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`. Worker-manager маппит их в agent-specific файлы через `get_instruction_path()`: Claude → `CLAUDE.md`, Factory → `AGENTS.md`. Также инжектится `TASK.md` с конкретной задачей.

### Project Status (Статус проекта)
Жизненный цикл проекта. Минимальный набор: `draft` → `active` → `paused` / `archived`. Не содержит процессных статусов (scaffolding, deploying) — активность определяется дочерними сущностями (Story, Run).

### Application (Приложение)
Runtime-сущность, связывающая репозиторий с сервером. Одно приложение = один deployable unit на конкретном сервере. Уникально по паре `(repo_id, server_handle)`. Трекает runtime-статус через `ApplicationStatus`.
**Статусы:** `not_deployed`, `deploying`, `running`, `stopping`, `stopped`, `undeploying`, `down`, `degraded`
**Связи:** Repository (repo_id), Server (server_handle)
**Таблица:** `applications`

### Deployment (Деплоймент)
Immutable запись попытки деплоя. Каждый deploy создаёт новую запись. Связан с Application через `application_id`. Результат фиксируется через `DeploymentResult`: `pending`, `success`, `failed`, `canceled`.
**Таблица:** `service_deployments`

### Service Status (Статус сервиса)
Runtime-состояние задеплоенного сервиса проекта. Отдельно от lifecycle-статуса проекта. Значения: `not_deployed`, `running`, `degraded`, `down`, `stopped`. Хранится в `project.service_status`.

### Repository Status (Статус репозитория)
Доступность git-репозитория. Значения: `active` (доступен на GitHub), `missing` (удалён или недоступен). Заменяет старый `ProjectStatus.MISSING`.

### Service Agent (Сервисный Агент)
LangGraph ReactAgent, живущий внутри сервиса langgraph, выполняющий специализированную профильную работу с доступом к инструментам консьюмера.
В отличие от CLI-агента, не использует изолированный Docker-контейнер и является частью долгоживущего процесса.

**Примеры:** Product Owner, Architect (располагаются в `services/langgraph/src/agents/`)

### Product Owner (PO)
Сервисный агент в langgraph-сервисе (`services/langgraph/src/agents/po/`).
- Принимает сообщения через `po:input`, отвечает через `po:response:{request_id}`.
- Использует Python @tool функции для вызова API и Redis.
- PostgreSQL checkpointer для сохранения контекста между сообщениями (per-user thread).
- Делегирует задачи в "отделы" (Engineering, DevOps).

### Architect (Архитектор)
Сервисный агент в langgraph-сервисе (`services/langgraph/src/agents/architect/`).
- Одноразовый (one-shot) агент, слушающий `architect:queue`.
- Занимается анализом фичей (Stories) из базы данных и их декомпозицией на конкретные задачи разработки (Tasks).

### CLI-Agent (CLI-Агент)
AI который работает внутри Developer Worker контейнера.
**Реализации:** Claude Code, Factory.ai Droid.

**Отличие от Service Agent:** CLI-Agent — это "личность" в эфемерном контейнере с доступом к bash и ФС, а Service Agent — нода в графе langgraph-сервиса, общающаяся через @tool.

### Engineering vs Developer

**Engineering** — подграф LangGraph, абстракция "отдела разработки".
**Developer** — конкретная нода внутри Engineering Subgraph.

| Термин | Уровень | Видимость | Описание |
|--------|---------|-----------|----------|
| **Engineering** | Subgraph | Внешний (PO) | Абстракция. PO ставит задачу "отделу", не зная внутренней структуры |
| **Developer** | Node/Worker | Внутренний | Конкретная реализация — воркер, который пишет код |

**Правило:** Термин "Developer" используется только когда обсуждаем реализацию внутри подграфа или конфигурацию воркера.

---

## Planning & Management

### Story (Пользовательская история)
Крупная фича или потребность пользователя. Генерирует одну или несколько Tasks. Живет на уровне всего проекта.
**Типы:** `product` (пользовательская ценность) | `technical` (внутренние инициативы, напр. Rust migration).
**Статусы:** `created` → `in_progress` → `pr_review` → `deploying` → `testing` → `completed` (также: `reopened`, `waiting_human_review`, `failed`, `archived`). `pr_review` — все задачи выполнены, PR создан из story branch в main, ожидание CI + auto-merge. `deploying` — deploy gate: story ждёт успешного деплоя. `testing` — задеплоенный сервис проходит QA тестирование. `waiting_human_review` — developer agent сообщил о блокере; ожидание вмешательства админа. `reopened` — пользователь сообщил о проблеме с completed/failed story; architect пересматривает и создаёт fix-задачи.
**Таблица:** `stories`

### Epic (Эпик)
Группировка Story через `parent_story_id` для очень крупных фич.

### Repository (Репозиторий)
Сущность в БД, связывающая код с конкретным git-репозиторием. Каждый репозиторий имеет `provider_repo_id` (GitHub ID) и `git_url`.
**Таблица:** `repositories`

### Task (Задача)
*(Бывший WorkItem)*. Единица планирования работы для разработчика/агента.
**Статусы:** `backlog` → `todo` → `in_dev` → `in_ci` → `testing` → `done` (также: `blocked`, `waiting_human_review`, `failed`, `cancelled`)
`waiting_human_review` — developer agent сообщил о блокере через `POST localhost:9090/result` с `{"success": false, "reason": "..."}`. Pipeline приостановлен до вмешательства админа (`POST /tasks/{id}/resume`).
**Связи:** Story (опционально), Repository (NOT NULL), Project.
**Таблица:** `tasks`

### Brainstorm (Мозговой штурм)
Запись в БД для обсуждения технических решений до начала кодинга.
**Таблица:** `brainstorms`

## Data & Messaging

### Run (Запуск)
*(Бывший Task)*. Сущность в PostgreSQL. Отслеживает выполнение асинхронной работы (engineering, deploy, QA).
**Типы:** `RunType` — `ENGINEERING`, `DEPLOY`, `QA`
**Статусы:** `QUEUED` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`

**Связи:** Project, Story (optional, via `story_id` FK)

**Таблица:** `tasks` (в процессе переименования)

### Message (Сообщение)
Данные в Redis Stream queue. Содержит `task_id` и параметры для обработки.

**Не путать с:** Event (уведомление о прогрессе)

**Формат:** JSON обёрнутый в `{"data": "..."}`

### Event (Событие)
Уведомление о прогрессе выполнения Run. Публикуется в `callback_stream`.

**Типы:** `started`, `progress`, `completed`, `failed`

**Используется для:** Показа прогресса пользователю в Telegram

---

## LangGraph

### Node (Нода)
Узел LangGraph графа. Функция или класс обрабатывающий State.

**Типы:**
- Функциональная нода — простая async функция
- LLM нода — использует LLM для принятия решений
- Tool executor — выполняет вызовы Tools

**Примеры:** `DeveloperNode`, `DeployerNode`, `TesterNode`

### Subgraph (Подграф)
Группа связанных Nodes, выполняющих определённый этап работы.

**Примеры:**
- Engineering Subgraph — Developer → Tester
- DevOps Subgraph — EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer (triggers GitHub Actions)

### Tool (Инструмент)
Функция доступная LLM для вызова. Декоратор `@tool`.

**Примеры:** `create_github_repo`, `allocate_port`, `get_server_info`

---

## Background Processing

### Background Task (Фоновая задача)
Периодическая задача в scheduler. Cron-like выполнение.

**Примеры:**
- `github_sync` — синхронизация репозиториев
- `server_sync` — синхронизация статусов серверов
- `health_checker` — проверка здоровья серверов

---

## Queues

### Job Queue (Очередь задач)
Redis Stream для асинхронной обработки. Consumer читает Messages из очереди.

**Очереди:**
- `engineering:queue` — задачи на разработку
- `deploy:queue` — задачи на деплой
- `provisioner:queue` — провизия серверов


### Command Queue (Очередь команд)
Redis Stream для управления Workers.

**Очереди:**
- `worker:commands` — команды для worker-manager (create, delete)
- `worker:responses:developer` — ответы от worker-manager для Developer воркеров

### Story Worker Registry
Redis Hash `story:workers` — маппинг `story_id → worker_id`. Engineering consumer записывает после первого spawn, читает для последующих задач в story. Scheduler очищает при завершении или провале story.

### Callback Stream
Redis Stream для Events прогресса конкретного Run.

**Формат имени:** `task_progress:{task_id}` (пока использует префикс task)

---

## Visual Summary

```
┌──────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   User  ──────►  Telegram Bot (Service)                             │
│                        │                                             │
│                        ▼                                             │
│                  API (Service)  ◄────►  PostgreSQL                  │
│                        │                                             │
│                        ▼                                             │
│              ┌─── Run (DB entity) ────┐                             │
│              │                        │                              │
│              ▼                        ▼                              │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │ engineering:queue │    │   deploy:queue   │                     │
│    └────────┬─────────┘    └────────┬─────────┘                     │
│             │                       │                                │
│             ▼                       ▼                                │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │   Engineering    │    │     DevOps       │                     │
│    │    Subgraph      │    │    Subgraph      │                     │
│    │   (LangGraph)    │    │   (LangGraph)    │                     │
│    └────────┬─────────┘    └────────┬─────────┘                     │
│             │                       │                                │
│             ▼                       ▼                                │
│    ┌──────────────────┐    ┌──────────────────┐                     │
│    │  Worker Manager  │    │  GitHub Actions  │                     │
│    │  (containers)    │    │  (deploy.yml)    │                     │
│    └────────┬─────────┘    └─────────────────-┘                     │
│             │                                                        │
│    ┌────────┼────────────┐                                          │
│    ▼        ▼            ▼                                           │
│ ┌────────┐ ┌────────┐ ┌────────┐                                    │
│ │ Worker │ │ Worker │ │ Worker │  (ephemeral, Developer only)       │
│ │(Claude)│ │(Claude)│ │(Droid) │                                    │
│ └────────┘ └────────┘ └────────┘                                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```
