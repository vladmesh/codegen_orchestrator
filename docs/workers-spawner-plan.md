# Workers-Spawner Implementation Plan

План реализации унифицированного сервиса управления агентами.

## Phase 1: Orchestrator CLI Refactoring ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Подготовить универсальный инструмент для агентов с поддержкой прав доступа.

**Реализовано:**
- CLI находится в `services/agent-worker/cli/`
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

## Phase 3: Base Image

**Статус:** Не начато

**Цель:** Универсальный Docker-образ для всех агентов.

1. **Create `services/universal-worker/`:**
    - `Dockerfile` — Ubuntu + Python + Node.js + базовые утилиты.
    - Pre-install `orchestrator-cli`.

2. **Entrypoint:**
    - Принимает install_commands и agent_command через ENV.
    - Выполняет установку при старте.
    - Запускает shell в интерактивном режиме.

3. **Build & Push:**
    - Добавить в `docker-compose.yml` для локального использования.
    - Image tag: `universal-worker:latest`.

---

## Phase 4: Workers-Spawner Service

**Статус:** Не начато (структура сервиса создана в Phase 2)

**Цель:** Создать Redis API для управления контейнерами.

1. **Redis API Handlers:**
    - `cli-agent.create` → собрать container config через factories → docker run
    - `cli-agent.send_command` → docker exec / stdin
    - `cli-agent.send_file` → docker exec echo/cat или cp
    - `cli-agent.status` → docker inspect
    - `cli-agent.logs` → docker logs
    - `cli-agent.delete` → docker rm

2. **Container Lifecycle:**
    - `docker pause` / `unpause` для idle containers.
    - TTL tracking и auto-cleanup.
    - Health checks.
    - Валидация входящего JSON конфига (JSON Schema).

3. **Events:**
    - PubSub publish:
        - `agents:{id}:response` — от orchestrator respond
        - `agents:{id}:command_exit` — завершение команды
        - `agents:{id}:status` — изменение state

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
| 3. Base Image | 0.5 day | |
| 4. Spawner Service | 2 days | |
| 5. Migration | 1-2 days | |
| 6. Cleanup | 0.5 day | |
| **Total** | **~1 week** | |

---

## Open Questions

1. **Image caching** — На MVP один base image. Потом можно кэшировать pre-built layers для популярных комбинаций.

2. **New agents/capabilities** — Добавление нового агента = новый класс-фабрика + регистрация в enum. Минимально инвазивно.

3. **Claude skills** — Генерация `~/.claude/skills/` для Claude — специфика ClaudeCodeAgent фабрики.
