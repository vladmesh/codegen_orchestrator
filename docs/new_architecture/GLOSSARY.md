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
- `infra-service` — consumer для `provisioner:queue`, `ansible:deploy:queue`
- `scaffolder` — consumer для `scaffolder:queue`
- `worker-wrapper` — consumer для `worker:*:input` (внутри контейнера воркера)

> **Важно:** Не путайте с именем сервиса. Нет сервиса `engineering-consumer` — есть сервис `langgraph`, который является consumer'ом очереди `engineering:queue`.

### Worker (Воркер)
Контейнер с CLI-Agent внутри.

**Типы:**

| Type | Lifecycle | Queue Pattern | Session |
|------|-----------|---------------|---------|
| **PO Worker** | Long-lived (per user) | `worker:po:{worker_id}:*` | Yes |
| **Developer Worker** | Ephemeral (per task) | `worker:developer:*` | No (stateless) |

1.  **Product Owner (PO)** — Единая точка входа. Общается с юзером, управляет проектами через CLI. Сессия сохраняется между сообщениями.
2.  **Developer Worker** — Эфемерный. Выполняет одну задачу и завершается. Stateless — контекст это код в репо + ошибки.

**Управляется:** `worker-manager`
**Конфигурация:** Промпты берутся из `CLAUDE.md` (для Claude) или `AGENTS.md` (общее/другие).

### Product Owner (PO)
Специализированный Worker, который:
- Принимает сообщения от пользователя (Telegram).
- Оркестрирует процесс через CLI (`orchestrator`).
- Делегирует задачи в "отделы" (Engineering, DevOps).
- Не знает внутренней кухни отделов (Black Box).

### CLI-Agent (CLI-Агент)
AI который работает внутри Worker.
**Реализации:**
- **Product Owner Agent**: Умный, знает контекст проекта, рулит процессом.
- **Coding Agent**: Узкоспециализированный (внутри Engineering Subgraph, если используется).

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

**Примеры:** `DeveloperNode`, `DeployerNode`, `AnalystNode`

### Subgraph (Подграф)
Группа связанных Nodes, выполняющих определённый этап работы.

**Примеры:**
- Engineering Subgraph — Developer → Tester
- DevOps Subgraph — EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer

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
- `ansible:deploy:queue` — делегированный Ansible деплой

### Command Queue (Очередь команд)
Redis Stream для управления Workers.

**Очереди:**
- `worker:commands` — команды для worker-manager (create, send_message, delete)
- `worker:responses:po` — ответы от worker-manager для PO воркеров
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
