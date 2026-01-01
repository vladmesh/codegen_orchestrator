# Workers-Spawner Implementation Plan

План реализации унифицированного сервиса управления агентами.

## Phase 1: Orchestrator CLI Refactoring ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Подготовить универсальный инструмент для агентов с поддержкой прав доступа.

**Реализовано:**
- CLI перемещён в `shared/cli/`
- `PermissionManager` читает `ORCHESTRATOR_ALLOWED_TOOLS` env var
- Декоратор `@require_permission("tool_name")` на всех командах
- Все команды поддерживают `--json` output
- 22 unit теста (`make test-orchestrator-cli`)

---

## Phase 2: Agent & Capability Factories ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Создать фабрики для преобразования декларативных конфигов в действия.

**Реализовано:**
- Сервис `services/workers-spawner/`
- Models: `WorkerConfig`, `AgentType`, `CapabilityType`
- Agent Factories: `ClaudeCodeAgent`, `FactoryDroidAgent` (stub)
- Capability Factories: `GitCapability`, `CurlCapability`, `NodeCapability`, `PythonCapability`
- `ConfigParser` для преобразования конфига в Docker команды
- 23 unit теста (`make test-workers-spawner`)

---

## Phase 3: Base Image ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Универсальный Docker-образ для всех агентов.

**Реализовано:**
- `services/universal-worker/Dockerfile` — Ubuntu 24.04 + Python 3.12 + Node.js
- Pre-installed `orchestrator-cli` из `shared/cli/`
- `entrypoint.sh` — динамическая настройка через ENV
- Build target в docker-compose: `docker compose build universal-worker`
- Image tag: `universal-worker:latest`

---

## Phase 4: Workers-Spawner Service ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Redis API для управления контейнерами.

**Реализовано:**
- `container_service.py` — Docker subprocess management (create, exec, logs, delete, pause/unpause)
- `redis_handlers.py` — Command routing (create, send_command, send_file, status, logs, delete)
- `lifecycle_manager.py` — TTL tracking, auto-pause idle containers
- `events.py` — PubSub publishing (response, command_exit, status)
- `main.py` — Redis stream consumer
- Dockerfile и docker-compose.yml настроены
- 23 unit теста (`make test-workers-spawner`)

---

## Phase 5: Migration

**Статус:** Не начато

**Цель:** Переключить существующие системы на новый spawner.

1. **Migrate Product Owner:**
    - Telegram Bot → `cli-agent.create` с JSON конфигом.
    - Входящие сообщения → `cli-agent.send_command`.
    - Подписка на `agents:{id}:response` для ответов.

2. **Migrate Developer Node:**
    - LangGraph Developer node → `cli-agent.create`.
    - Контекст проекта → `cli-agent.send_file` (AGENTS.md, TASK.md).
    - Запуск задачи → `cli-agent.send_command`.

---

## Phase 6: Cleanup

**Статус:** Не начато

1. Remove `services/agent-spawner`.
2. Remove `services/worker-spawner`.
3. Update documentation.

---

## Timeline Estimate

| Phase | Effort | Status |
|-------|--------|--------|
| 1. Orchestrator CLI | 0.5 day | ✅ Done |
| 2. Factories | 1 day | ✅ Done |
| 3. Base Image | 0.5 day | ✅ Done |
| 4. Spawner Service | 2 days | ✅ Done |
| 5. Migration | 1-2 days | |
| 6. Cleanup | 0.5 day | |
| **Total** | **~1 week** | |

---

## Open Questions

1. **Image caching** — На MVP один base image. Потом можно кэшировать pre-built layers для популярных комбинаций.

2. **New agents/capabilities** — Добавление нового агента = новый класс-фабрика + регистрация в enum. Минимально инвазивно.

3. **Claude skills** — Генерация `~/.claude/skills/` для Claude — специфика ClaudeCodeAgent фабрики.
