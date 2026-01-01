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
- Agent Factories:
  - `ClaudeCodeAgent` — установка Claude Code CLI и настройка окружения
  - `FactoryDroidAgent` — установка Factory.ai Droid CLI через официальный installer
- Capability Factories: `GitCapability`, `CurlCapability`, `NodeCapability`, `PythonCapability`, `DockerCapability`
- `ConfigParser` для преобразования конфига в Docker команды
- 23 unit теста (`make test-workers-spawner`)

---

## Phase 3: Base Image ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Универсальный Docker-образ для всех агентов.

**Реализовано:**
- `services/universal-worker/Dockerfile`:
  - Ubuntu 24.04 + Python 3.12 + Node.js
  - Non-root user `worker` (uid 1000) для volume mount совместимости
  - Pre-installed `orchestrator-cli` из `shared/cli/`
- `entrypoint.sh`:
  - Динамическая настройка через ENV (`INSTALL_COMMANDS`, `AGENT_COMMAND`)
  - Daemon mode (`tail -f /dev/null`) для persistent containers
  - Контейнеры получают команды через `docker exec`
- Build target в docker-compose: `docker compose build universal-worker`
- Image tag: `universal-worker:latest`

---

## Phase 4: Workers-Spawner Service ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Redis API для управления контейнерами.

**Реализовано:**
- `container_service.py`:
  - Docker subprocess management (create, exec, logs, delete, pause/unpause)
  - Auto-injection env vars из spawner окружения
  - OAuth session volume mounting для Claude Code (`HOST_CLAUDE_DIR`)
  - Sysbox runtime для Docker-in-Docker capability
- `redis_handlers.py` — Command routing (create, send_command, send_file, status, logs, delete)
- `lifecycle_manager.py` — TTL tracking, auto-pause idle containers
- `events.py` — PubSub publishing (response, command_exit, status)
- `main.py` — Redis stream consumer (`cli-agent:commands` → `cli-agent:responses`)
- Dockerfile и docker-compose.yml настроены
- 23 unit теста (`make test-workers-spawner`)

**Environment Variables:**
- `FACTORY_API_KEY` — Factory.ai authentication
- `ANTHROPIC_API_KEY` — Claude API key (опционально, при отсутствии OAuth session)
- `HOST_CLAUDE_DIR` — путь к `~/.claude` на хосте для session mount

---

## Phase 5: Migration ✅

**Статус:** Завершено (2026-01-01)

**Цель:** Переключить существующие системы на новый spawner.

**Реализовано:**

1. **Telegram Bot Migration:**
   - Telegram сообщения направляются на workers-spawner через Redis
   - Claude Code в контейнере выполняет роль Product Owner
   - Интеграция через `cli-agent:commands` stream

2. **LangGraph Developer Node Migration:**
   - Developer node использует workers-spawner для coding tasks
   - Передача контекста через `AGENTS.md`, `TASK.md`
   - Factory Droid и Claude Code как coding agents

---

## Phase 6: Cleanup ✅

**Статус:** Завершено (2026-01-02)

**Задачи выполнены:**
1. ✅ Remove `services/agent-spawner`
2. ✅ Remove `services/worker-spawner`
3. ✅ Remove `services/agent-worker`
4. ✅ Remove `services/coding-worker`
5. ✅ Remove dead code (`coding_worker.py`, `test_agent_bridge.py`)
6. ✅ Update documentation (CLAUDE.md, ARCHITECTURE.md, AGENTS.md)
7. ✅ Update build files (Makefile, docker-compose.test.yml)

---

## Phase 7: Testing ✅

**Статус:** Завершено (2026-01-02)

**Цель:** Проверить работоспособность обоих агентов через Redis API.

**Результаты тестирования:**

### Factory Droid
- ✅ Создание агента через Redis API
- ✅ Установка CLI через official installer
- ✅ Аутентификация через `FACTORY_API_KEY`
- ✅ LLM ответы на вопросы:
  - "What is 5 + 7?" → "12"
  - "Explain in one sentence what Docker is" → успешный ответ

### Claude Code
- ✅ Создание агента с session volume mount
- ✅ Установка Claude CLI
- ✅ OAuth аутентификация через mounted `~/.claude`
- ✅ LLM ответы на вопросы:
  - "What is 10 + 5?" → "15"
- ✅ Корректная работа без `ANTHROPIC_API_KEY` (OAuth приоритетнее)

**Архитектура:**
```
Telegram → workers-spawner → Claude Code container (PO role)
                           → triggers LangGraph subgraphs

LangGraph → workers-spawner → Factory Droid/Claude Code containers
                           → coding tasks execution
```

---

## Summary

Workers-spawner успешно реализован и протестирован:
- ✅ Универсальный base image для любых CLI агентов
- ✅ Декларативная конфигурация через фабрики
- ✅ Redis API для управления lifecycle
- ✅ Auto-injection secrets из окружения
- ✅ Session volume mounting для Claude OAuth
- ✅ Sysbox integration для Docker-in-Docker
- ✅ Миграция Telegram Bot и LangGraph
- ✅ End-to-end тестирование обоих агентов

**Осталось:**
- Phase 6: Удаление legacy spawners
