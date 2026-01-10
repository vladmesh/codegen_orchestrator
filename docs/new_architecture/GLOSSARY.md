# Glossary / Словарь терминов

Единая терминология для проекта codegen_orchestrator.

## Core Concepts

### Service (Сервис)
Долгоживущий процесс. Один контейнер = один сервис.

**Примеры:** `api`, `telegram-bot`, `langgraph`, `scheduler`

### Consumer (Консьюмер)
Сервис, который слушает Redis queue и обрабатывает Messages.
Ранее назывались `*-worker`, но это создавало путаницу с Worker.

**Примеры:**
- `engineering-consumer` — обрабатывает engineering:queue
- `deploy-consumer` — обрабатывает deploy:queue
- `infra-consumer` — обрабатывает provisioner:queue и ansible:deploy:queue
- `scaffolder` — обрабатывает scaffolder:queue

### Worker (Воркер)
Эфемерный контейнер с CLI-Agent внутри.
**Типы:**
1.  **Product Owner (PO)** — Единая точка входа. Общается с юзером, управляет проектами через CLI.
2.  **Task Worker** — (Legacy/Specific) Выполняет конкретную задачу если нужно (напр. написать код), но чаще это скрыто внутри подграфов.

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
- `worker:responses` — ответы от worker-manager

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
