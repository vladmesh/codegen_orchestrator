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
- `scaffolder` — consumer для `scaffolder:queue`
- `worker-wrapper` — consumer для `worker:*:input` (внутри контейнера воркера)

> **Важно:** Не путайте с именем сервиса. Нет сервиса `engineering-consumer` — есть сервис `langgraph`, который является consumer'ом очереди `engineering:queue`.

### Worker (Воркер)
Docker-контейнер с CLI coding agent внутри. Используется только для Developer workers.

| Type | Lifecycle | Queue Pattern | Session |
|------|-----------|---------------|---------|
| **Developer Worker** | Ephemeral (per task) | `worker:{worker_id}:*` | No (stateless) |

**Developer Worker** — Эфемерный. Выполняет одну задачу и завершается. Stateless — контекст это код в репо + ошибки.

**Управляется:** `worker-manager`
**Конфигурация:** Промпты хранятся в `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md`. Worker-manager маппит их в agent-specific файлы через `get_instruction_path()`: Claude → `CLAUDE.md`, Factory → `AGENTS.md`. Также инжектится `TASK.md` с конкретной задачей.

### Product Owner (PO)
LangGraph ReactAgent в langgraph-сервисе (`services/langgraph/src/po/`).
- Принимает сообщения через `po:input`, отвечает через `po:response:{request_id}`.
- Использует Python @tool функции для вызова API и Redis.
- PostgreSQL checkpointer для сохранения контекста между сообщениями (per-user thread).
- Делегирует задачи в "отделы" (Engineering, DevOps).
- Не использует контейнер, CLI, Docker.

### CLI-Agent (CLI-Агент)
AI который работает внутри Developer Worker контейнера.
**Реализации:** Claude Code, Factory.ai Droid.

**Отличие от LangGraph Agent:** CLI-Agent — это "личность" в контейнере. LangGraph Node — это "функция" в графе.

### Engineering vs Developer

**Engineering** — подграф LangGraph, абстракция "отдела разработки".
**Developer** — конкретная нода внутри Engineering Subgraph.

| Термин | Уровень | Видимость | Описание |
|--------|---------|-----------|----------|
| **Engineering** | Subgraph | Внешний (PO) | Абстракция. PO ставит задачу "отделу", не зная внутренней структуры |
| **Developer** | Node/Worker | Внутренний | Конкретная реализация — воркер, который пишет код |

**Правило:** Термин "Developer" используется только когда обсуждаем реализацию внутри подграфа или конфигурацию воркера.

---

## Data & Messaging

### Task (Задача)
Сущность в PostgreSQL. Отслеживает lifecycle асинхронной работы.

**Статусы:** `QUEUED` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED`

**Связи:** Project, User

**Таблица:** `tasks`

### Message (Сообщение)
Данные в Redis Stream queue. Содержит `task_id` и параметры для обработки.

**Не путать с:** Event (уведомление о прогрессе)

**Формат:** JSON обёрнутый в `{"data": "..."}`

### Event (Событие)
Уведомление о прогрессе выполнения Task. Публикуется в `callback_stream`.

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
- `scaffolder:queue` — scaffolding проектов
- `provisioner:queue` — провизия серверов


### Command Queue (Очередь команд)
Redis Stream для управления Workers.

**Очереди:**
- `worker:commands` — команды для worker-manager (create, delete)
- `worker:responses:developer` — ответы от worker-manager для Developer воркеров

### Callback Stream
Redis Stream для Events прогресса конкретной Task.

**Формат имени:** `task_progress:{task_id}`

---

## Visual Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   User  ──────►  Telegram Bot (Service)                        │
│                        │                                        │
│                        ▼                                        │
│                  API (Service)  ◄────►  PostgreSQL             │
│                        │                                        │
│                        ▼                                        │
│              ┌─── Task (DB entity) ───┐                        │
│              │                        │                         │
│              ▼                        ▼                         │
│    ┌──────────────────┐    ┌──────────────────┐                │
│    │ engineering:queue│    │   deploy:queue   │                │
│    └────────┬─────────┘    └────────┬─────────┘                │
│             │                       │                           │
│             ▼                       ▼                           │
│    ┌──────────────────┐    ┌──────────────────┐                │
│    │   Engineering    │    │      Deploy      │                │
│    │    Consumer      │    │     Consumer     │                │
│    └────────┬─────────┘    └────────┬─────────┘                │
│             │                       │                           │
│             ▼                       ▼                           │
│    ┌──────────────────┐    ┌──────────────────┐                │
│    │   Engineering    │    │     DevOps       │                │
│    │    Subgraph      │    │    Subgraph      │                │
│    │   (LangGraph)    │    │   (LangGraph)    │                │
│    └────────┬─────────┘    └────────┬─────────┘                │
│             │                       │                           │
│             ▼                       ▼                           │
│    ┌──────────────────────────────────────────┐                │
│    │           Worker Manager                  │                │
│    │    (creates/destroys Workers)            │                │
│    └────────────────────┬─────────────────────┘                │
│                         │                                       │
│            ┌────────────┼────────────┐                         │
│            ▼            ▼            ▼                          │
│       ┌────────┐   ┌────────┐   ┌────────┐                     │
│       │ Worker │   │ Worker │   │ Worker │   (ephemeral)       │
│       │(Claude)│   │(Claude)│   │(Droid) │                     │
│       └────────┘   └────────┘   └────────┘                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```
