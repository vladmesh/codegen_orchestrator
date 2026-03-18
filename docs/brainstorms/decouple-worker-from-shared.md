# Brainstorm: Decouple worker containers from orchestrator shared package

> **Дата**: 2026-03-18
> **Контекст**: Worker containers тянут весь shared пакет оркестратора, что конфликтует с кодом проекта
> **Status**: done

---

## Current State

Worker-контейнер общается с оркестратором через два механизма:

1. **worker-wrapper** (entrypoint) — Redis Streams (input/output). Импорты из shared:
   - `shared.log_config.config.setup_logging`
   - `shared.redis.client.RedisStreamClient`
   - `shared.contracts.queues.worker_lifecycle.WorkerLifecycleEvent`

2. **orchestrator-cli** (CLI для агента внутри контейнера). Импорты из shared:
   - `shared.config.BaseSettings`
   - `shared.contracts.dto.project.ProjectStatus, ServiceModule`
   - `shared.crypto.decrypt_dict, encrypt_dict`
   - `shared.queues.PO_INPUT_QUEUE`

Shared копируется в `/opt/orch/shared`, PYTHONPATH включает `/opt/orch`. Проблема: если генерируемый проект тоже имеет `shared/` директорию (а service-template её создаёт), возникает конфликт имён. Воркер видит `/app/shared` от оркестратора вместо `./shared` от проекта.

Хотфикс: `PYTHONPATH=.` в Makefile проекта перекрывает, но это хрупко. Каждый е2е прогон ловит этот баг.

## Problem

1. **Конфликт имён**: `shared` — generic имя. Любой проект с `shared/` пакетом сломается.
2. **Инвазивность**: В worker-контейнер копируется весь `shared/` (~20 файлов, включая models/, clients/, schemas/), хотя реально нужно 5-6 функций.
3. **Coupling**: Изменения в shared ломают worker image — нужен rebuild. А shared меняется часто.
4. **Концептуальный**: воркер — это изолированная среда для чужого кода. Чем меньше в ней нашего кода, тем лучше.

## Решение: localhost HTTP-сервер в worker-wrapper

### Ключевой инсайт

Воркеру (агенту) реально нужно уметь:
1. Сказать "готово" (+ commit, summary)
2. Сказать "не получилось" (+ причина)
3. Сказать "заблокирован, нужен человек" (+ причина)

Всё. Никаких `project list`, `engineering trigger`, `deploy` — эти переходы происходят автоматически в оркестраторе после получения результата.

Сейчас агент пишет `<result>JSON</result>` в stdout → wrapper парсит → если мусор, то поздно, агент уже завершился. С HTTP-эндпоинтом агент дёргает `POST /complete` → получает 200 или 400 → может исправиться и попробовать снова.

### Почему в wrapper, а не отдельный сервис

- **worker-manager** — его зона ответственности это инфраструктура контейнеров (создать, убить, логи). Бизнес-логика результатов — не его дело.
- **Отдельный сервис** — придётся тащить shared (RedisStreamClient), ещё один контейнер в docker-compose, ещё одна сеть.
- **worker-wrapper** — уже в контейнере, уже имеет Redis-клиент, уже знает worker_id. Поднять localhost HTTP-сервер в отдельном asyncio task — тривиально. Shared не расползается.

### Архитектура

```
┌──────────────────────────────────────────────────────┐
│  Worker Container                                    │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ worker-wrapper (entrypoint)                    │  │
│  │                                                │  │
│  │  ┌─────────────┐    ┌───────────────────────┐  │  │
│  │  │ Agent Runner │    │ HTTP server            │  │  │
│  │  │ (asyncio)   │    │ localhost:9090          │  │  │
│  │  │             │    │                         │  │  │
│  │  │ - run agent │    │ POST /complete          │  │  │
│  │  │ - watchdog  │    │ POST /failed            │  │  │
│  │  │ - timeout   │    │ POST /blocker           │  │  │
│  │  └──────┬──────┘    └────────┬────────────────┘  │  │
│  │         │                    │                    │  │
│  │         │   Redis ←──────────┘                    │  │
│  │         │   (publish result)                      │  │
│  │         │                                         │  │
│  │         ▼                                         │  │
│  │   Redis Streams                                   │  │
│  │   - worker:{id}:input  (получить задачу)          │  │
│  │   - worker:{id}:output (результат)                │  │
│  │   - lifecycle events   (started, failed on crash) │  │
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ Agent (Claude CLI) — subprocess                │  │
│  │                                                │  │
│  │  curl POST localhost:9090/complete             │  │
│  │  curl POST localhost:9090/blocker              │  │
│  │                                                │  │
│  │  Ноль Python-зависимостей от оркестратора      │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

### Разделение ответственности

**worker-wrapper** (наш код, доверенный):
- Получить задачу из Redis stream
- Подготовить workspace (git pull, TASK.md, STORY.md)
- Запустить агента как subprocess
- **HTTP-сервер на localhost:9090** — принимает результаты от агента, валидирует, публикует в Redis
- **Watchdog**: процесс завершился → проверить "результат уже получен?" → если нет — publish `failed`
- Session management (Claude resume)
- **Больше не парсит stdout**

**Агент** (Claude CLI, чужой код):
- Пишет код, запускает тесты
- Когда готов — `curl POST localhost:9090/complete -d '{...}'`
- Если заблокирован — `curl POST localhost:9090/blocker -d '{...}'`
- Инструкции в INSTRUCTIONS.md: "если получил 400 — исправь формат и попробуй снова"
- **Ноль Python-зависимостей от оркестратора**

### HTTP API (localhost:9090)

```
POST /complete
  Body: { "commit": "abc123", "summary": "..." }
  → validates → xadd worker:{id}:output { status: completed, ... }
  → 200 OK / 400 Bad Request

POST /failed
  Body: { "reason": "tests don't pass after 3 attempts" }
  → validates → xadd worker:{id}:output { status: failed, ... }
  → 200 OK / 400 Bad Request

POST /blocker
  Body: { "reason": "spec ambiguous, need clarification on X" }
  → validates → xadd worker:{id}:output { status: blocked, ... }
  → 200 OK / 400 Bad Request
```

worker_id не в URL — wrapper и так его знает из env. Агент просто дёргает localhost.

### Что происходит с orchestrator-cli

**Удаляется** как Python-пакет. Заменяется строчкой в INSTRUCTIONS.md:

```markdown
## Reporting results

When done, report via HTTP (localhost:9090 is always available):
- Success: `curl -sf -X POST localhost:9090/complete -H 'Content-Type: application/json' -d '{"commit":"<hash>","summary":"<what you did>"}'`
- Failure: `curl -sf -X POST localhost:9090/failed -H 'Content-Type: application/json' -d '{"reason":"<why>"}'`
- Blocked: `curl -sf -X POST localhost:9090/blocker -H 'Content-Type: application/json' -d '{"reason":"<what you need>"}'`

If you get 400 — fix the payload format and retry.
```

### Что происходит с shared в worker image

- **orchestrator-cli удалён** → его зависимости от shared исчезают
- **worker-wrapper** всё ещё использует shared (RedisStreamClient, logging) — это ок, wrapper наш доверенный код
- **shared остаётся в image**, но только для wrapper. Агент его не видит — subprocess запускается в `/workspace/` со своим PYTHONPATH
- Конфликт имён уходит сам: orchestrator-cli был единственным местом где агентский процесс импортировал shared. Без CLI агент — чистый subprocess с curl, ему shared не нужен

## Что НЕ меняется

- worker-wrapper остаётся entrypoint, продолжает читать задачи из Redis
- Redis streams — основной транспорт для задач и lifecycle
- worker-manager управляет контейнерами
- Агент по-прежнему работает с файлами (TASK.md, STORY.md, код)

## Open Questions

1. **Respond (сообщение пользователю)**: нужно ли агенту уметь писать пользователю напрямую, или достаточно поля в complete/blocker?
2. **Таймаут между complete и завершением процесса**: агент отправил complete, но Claude CLI ещё не вышел. Wrapper ждёт завершения процесса или убивает после grace period?

## Action Items

- → new task: "HTTP-сервер в worker-wrapper (localhost:9090) — complete, failed, blocker эндпоинты с валидацией и publish в Redis"
- → new task: "Удалить orchestrator-cli, перевести агента на curl к localhost:9090" (обновить INSTRUCTIONS.md, убрать пакет, обновить Dockerfile)
- → new task: "Убрать result_parser из wrapper, добавить watchdog-логику (проверка: результат получен через HTTP?)"
