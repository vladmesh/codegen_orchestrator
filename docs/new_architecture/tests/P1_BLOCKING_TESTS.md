# P1 Blocking Tests: Agent Installation & CLI Integration

**Status:** � PASSED — Ready for Phase 2
**Created:** 2026-01-13
**Related:** [MIGRATION_PLAN.md](../MIGRATION_PLAN.md), [CURRENT_GAPS.md](../CURRENT_GAPS.md)

---

## Overview

Эти тесты являются **жёстким блокером** перед переходом к Phase 2. Текущие интеграционные тесты проверяют только создание контейнеров и наличие capabilities (git/curl), но НЕ проверяют:

1. Установку агентов (Claude Code CLI / Factory Droid CLI)
2. Работу orchestrator-cli внутри контейнера
3. Реальное взаимодействие с LLM через Redis streams

---

## Test Series 1: Claude Agent Installation

**File:** `tests/integration/backend/test_claude_agent.py`

### Infrastructure

```yaml
services:
  - redis
  - db (postgres)
  - api
  - worker-manager
```

### Test Cases

#### 1.1 `test_claude_cli_installed`

**Цель:** Убедиться что Claude Code CLI установлен в контейнере.

```python
@pytest.mark.integration
async def test_claude_cli_installed(redis, docker_client):
    """Claude worker должен иметь установленный claude CLI."""
    # 1. Отправить CreateWorkerCommand(agent_type=CLAUDE)
    # 2. Дождаться CreateWorkerResponse
    # 3. docker exec: which claude
    # 4. docker exec: claude --version
    # 5. Assert exit_code == 0 и version выводится
```

#### 1.2 `test_claude_session_mounted`

**Цель:** Убедиться что host session примонтирована (если auth_mode=host_session).

```python
@pytest.mark.integration
async def test_claude_session_mounted(redis, docker_client):
    """При auth_mode=host_session директория ~/.claude должна быть примонтирована."""
    # 1. CreateWorkerCommand с auth_mode="host_session", host_claude_dir="/tmp/fake-claude"
    # 2. Проверить что /home/worker/.claude существует в контейнере
    # 3. Проверить что содержимое соответствует host директории
```

#### 1.3 `test_claude_instructions_injected`

**Цель:** Убедиться что CLAUDE.md создан с правильным содержимым.

```python
@pytest.mark.integration
async def test_claude_instructions_injected(redis, docker_client):
    """CLAUDE.md должен содержать переданные instructions."""
    # 1. CreateWorkerCommand с instructions="Test instructions content"
    # 2. docker exec: cat /workspace/CLAUDE.md
    # 3. Assert содержит "Test instructions content"
```

#### 1.4 `test_orchestrator_cli_installed`

**Цель:** Убедиться что orchestrator CLI доступен.

```python
@pytest.mark.integration
async def test_orchestrator_cli_installed(redis, docker_client):
    """orchestrator CLI должен быть установлен и доступен."""
    # 1. CreateWorkerCommand
    # 2. docker exec: which orchestrator
    # 3. docker exec: orchestrator --help
    # 4. Assert exit_code == 0
```

#### 1.5 `test_orchestrator_cli_create_project_valid`

**Цель:** Проверить что orchestrator CLI может создать проект.

```python
@pytest.mark.integration
async def test_orchestrator_cli_create_project_valid(redis, docker_client, api_client):
    """orchestrator project create должен создавать проект в API."""
    # 1. CreateWorkerCommand
    # 2. docker exec: orchestrator project create --name "test-project" --description "Test"
    # 3. Assert exit_code == 0
    # 4. API GET /projects → проект существует
```

#### 1.6 `test_orchestrator_cli_create_project_invalid`

**Цель:** Проверить что некорректные данные возвращают ошибку.

```python
@pytest.mark.integration
async def test_orchestrator_cli_create_project_invalid(redis, docker_client):
    """orchestrator project create с невалидными данными должен вернуть ошибку."""
    # 1. CreateWorkerCommand
    # 2. docker exec: orchestrator project create --name "" (пустое имя)
    # 3. Assert exit_code != 0
    # 4. Assert stderr содержит error message
```

#### 1.7 `test_orchestrator_cli_list_projects`

**Цель:** Проверить что можно получить список проектов.

```python
@pytest.mark.integration
async def test_orchestrator_cli_list_projects(redis, docker_client, api_client):
    """orchestrator project list должен возвращать созданные проекты."""
    # 1. Создать проект через API напрямую
    # 2. CreateWorkerCommand
    # 3. docker exec: orchestrator project list
    # 4. Assert созданный проект в выводе
```

---

## Test Series 2: Factory Agent Installation

**File:** `tests/integration/backend/test_factory_agent.py`

### Test Cases

#### 2.1 `test_factory_droid_installed`

**Цель:** Убедиться что droid CLI установлен.

```python
@pytest.mark.integration
async def test_factory_droid_installed(redis, docker_client):
    """Factory worker должен иметь установленный droid CLI."""
    # 1. CreateWorkerCommand(agent_type=FACTORY)
    # 2. docker exec: which droid
    # 3. docker exec: droid --version
    # 4. Assert exit_code == 0
```

#### 2.2 `test_factory_api_key_set`

**Цель:** Убедиться что FACTORY_API_KEY передан в контейнер.

```python
@pytest.mark.integration
async def test_factory_api_key_set(redis, docker_client):
    """FACTORY_API_KEY должен быть установлен в env."""
    # 1. CreateWorkerCommand с env_vars={"FACTORY_API_KEY": "fk-test-key"}
    # 2. docker exec: echo $FACTORY_API_KEY
    # 3. Assert == "fk-test-key"
```

#### 2.3 `test_factory_agents_md_injected`

**Цель:** Убедиться что AGENTS.md (не CLAUDE.md) создан.

```python
@pytest.mark.integration
async def test_factory_agents_md_injected(redis, docker_client):
    """Factory worker должен иметь AGENTS.md, а не CLAUDE.md."""
    # 1. CreateWorkerCommand(agent_type=FACTORY, instructions="Factory test")
    # 2. docker exec: cat /workspace/AGENTS.md → success
    # 3. docker exec: cat /workspace/CLAUDE.md → fail (не должен существовать)
```

#### 2.4-2.7 `test_orchestrator_cli_*`

Аналогичные тесты CLI как для Claude (create valid/invalid, list projects).

---

## Test Series 3: E2E Real LLM Tests (Skipped by Default)

**File:** `tests/e2e/test_real_llm.py`

**Marker:** `@pytest.mark.skip(reason="Requires real API keys")` или `@pytest.mark.e2e_real`

### Prerequisites

- Реальный `~/.claude` с активной сессией для Claude
- Реальный `FACTORY_API_KEY` для Factory
- Запуск: `pytest tests/e2e/test_real_llm.py --run-e2e-real`

### Test Cases

#### 3.1 `test_claude_real_session_deterministic_answer`

**Цель:** Проверить что Claude отвечает на детерминированный вопрос.

```python
@pytest.mark.e2e_real
@pytest.mark.skip(reason="Requires real Claude session")
async def test_claude_real_session_deterministic_answer(redis, docker_client):
    """
    Claude должен правильно ответить на математический вопрос.

    Flow:
    1. CreateWorkerCommand(agent_type=CLAUDE, auth_mode=host_session)
    2. Отправить в worker:{id}:input: "Ответь сколько будет шесть плюс три одним словом на русском языке"
    3. Читать из worker:{id}:output
    4. Assert "девять" in result.lower()
    """
    pass
```

#### 3.2 `test_claude_real_session_memory`

**Цель:** Проверить что Claude помнит предыдущий вопрос (session persistence).

```python
@pytest.mark.e2e_real
@pytest.mark.skip(reason="Requires real Claude session")
async def test_claude_real_session_memory(redis, docker_client):
    """
    Claude должен помнить предыдущий вопрос в рамках сессии.

    Flow:
    1. CreateWorkerCommand (тот же worker или новый с тем же session)
    2. Вопрос 1: "Ответь сколько будет шесть плюс три одним словом"
    3. Вопрос 2: "Верни предыдущий вопрос который я тебе задавал и только его"
    4. Assert "шесть" in result and "три" in result
    """
    pass
```

#### 3.3 `test_factory_api_key_deterministic_answer`

**Цель:** Проверить что Factory отвечает по API key.

```python
@pytest.mark.e2e_real
@pytest.mark.skip(reason="Requires real FACTORY_API_KEY")
async def test_factory_api_key_deterministic_answer(redis, docker_client):
    """
    Factory должен правильно ответить на математический вопрос.

    Flow:
    1. CreateWorkerCommand(agent_type=FACTORY, env_vars={FACTORY_API_KEY: real_key})
    2. Отправить: "Ответь сколько будет шесть плюс три одним словом на русском языке"
    3. Assert "девять" in result.lower()
    """
    pass
```

#### 3.4 `test_factory_api_key_session_memory`

**Цель:** Проверить что Factory помнит предыдущий вопрос.

```python
@pytest.mark.e2e_real
@pytest.mark.skip(reason="Requires real FACTORY_API_KEY")
async def test_factory_api_key_session_memory(redis, docker_client):
    """
    Factory должен помнить предыдущий вопрос.

    Flow: аналогично test_claude_real_session_memory
    """
    pass
```

---

## Implementation Checklist

### Перед написанием тестов нужно исправить:

- [x] **Gap 1:** `image_builder.py` — включить `agent.get_install_commands()` в Dockerfile
- [x] **Gap 2:** `worker-base/Dockerfile` — добавить Node.js
- [x] **Gap 3:** `contracts/worker.py` — добавить `auth_mode`, `host_claude_dir`, `api_key`
- [x] **Gap 4:** `consumer.py` — передавать auth config в manager
- [x] **Gap 5:** `container_config.py` — добавить `FACTORY_API_KEY`
- [x] **Gap 6:** `runners/claude.py` — добавить `--dangerously-skip-permissions`

### После исправлений:

- [x] Test Series 1: Claude Agent (7 тестов)
- [x] Test Series 2: Factory Agent (7 тестов)
- [ ] Test Series 3: E2E Real LLM (4 теста, skipped by default)

---

## Acceptance Criteria for Phase 2

**Phase 2 ЗАБЛОКИРОВАНА пока:**

1. ✅ Все тесты Series 1 проходят
2. ✅ Все тесты Series 2 проходят
3. ✅ E2E тесты могут быть запущены вручную с реальными ключами

**Команды для проверки:**

```bash
# Integration tests (должны проходить в CI)
make test-integration-backend

# E2E tests (ручной запуск с реальными ключами)
CLAUDE_SESSION_DIR=~/.claude FACTORY_API_KEY=fk-xxx pytest tests/e2e/test_real_llm.py --run-e2e-real -v
```

---

## Related Documents

- [CURRENT_GAPS.md](../CURRENT_GAPS.md) — Список gap'ов для исправления
- [MIGRATION_PLAN.md](../MIGRATION_PLAN.md) — Общий план миграции
- [worker_manager.md](../services/worker_manager.md) — Спецификация worker-manager
