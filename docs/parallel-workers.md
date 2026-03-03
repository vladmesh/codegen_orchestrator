# Параллельные Workers

Для кодогенерации используются изолированные Docker-контейнеры с AI coding agents.

## Текущая архитектура

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│          (Developer node в Engineering)             │
└─────────────────────────────────────────────────────┘
                         │
                  Redis streams
          (worker:commands / worker:{id}:*)
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│              worker-manager Service                 │
│    (API / Docker Client / Compose Proxy)            │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│        Worker Container (ephemeral)                 │
│  - Образ: worker-base-common + тулинг               │
│  - Нативные утилиты: ruff, pytest, make, python     │
│  - Выполняет coding task                            │
│  - Запрашивает инфраструктуру через CLI             │
└─────────────────────────────────────────────────────┘
```

## Изоляция и Flat Dev Environment

Вместо Docker-in-Docker (Sysbox), система использует парадигму **Flat Dev Environment**, управляемую с хоста через `worker-manager`. Это решает проблемы с RAM, кэшами слоёв и стабильностью.

1. **Dual-Network Setup**:
   Каждый воркер подключён к двум сетям:
   - `internal` (shared codegen network) — для связи с `api`, `redis` и `worker-manager`.
   - `dev_proj_<worker_id>` — изолированная сеть для сайдкар-контейнеров проекта.

2. **Compose Proxy**:
   Воркеры (инжектированные AI-агенты) **не имеют доступа к Docker**. Для запуска инфраструктурных зависимостей (DB, Redis) агенты вызывают `orchestrator dev-env start-infra db`, который проксирует запрос в `worker-manager`.

3. **Workspace Bind-Mount**:
   Код клонируется агентом внутрь `/workspace` директории в контейнере, которая примонтирована на хост. При наличии `project_id` путь: `/tmp/codegen/workspaces/<project_id>/workspace` (сохраняется между воркерами). Без `project_id`: `/tmp/codegen/workspaces/<worker_id>/workspace` (эфемерный). `docker compose` на хосте использует файлы из этого воркспейса для поднятия сайдкар-контейнеров.

## Запрет портов и конвенции

- **Никаких `ports:` в compose**: Сервисы шаблона не публикуют порты на хост, так как это вызовет конфликты при параллельной работе воркеров.
- Агенты обращаются к сайдкар-сервисам по именам хостов (`db:5432`) внутри изолированной сети `dev_proj_<worker_id>`.

## Worker Образы

Worker-base образ `worker-base-common` унифицирован:
- Ubuntu + Python 3.12 + Node.js
- **Shared Tooling Layer**: `ruff`, `pytest`, `mypy`, `copier`, и т.д. установлены на уровне образа (не дублируются на каждый воркер).
- Non-root user `worker` (uid 1000). Код на хосте через bind-mount не становится `root`-owned.
- Выполнение тестов и линтеров происходит **нативно** через per-service venv-ы, без запуска дополнительных эфемерных контейнеров.
