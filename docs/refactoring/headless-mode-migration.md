# Headless Mode Migration Plan

**Цель**: Мигрировать с persistent PTY mode на headless one-shot mode для чистого JSON вывода без визуального мусора.

**Дата создания**: 2026-01-08
**Статус**: Ready for implementation

---

## Обзор Проблемы

### Текущая Архитектура (Persistent PTY Mode)

```
Telegram Bot → workers-spawner.send_message_persistent(agent_id, message)
                    ↓
            ProcessManager.write_to_stdin(message)
                    ↓
            docker exec -it claude (PTY with pexpect)
                    ↓
            LogCollector reads stdout → cli-agent:responses stream
                    ↓
            ResponseListener → Telegram Bot
```

**Проблемы**:
- ❌ PTY создан для человека → прогресс-бары, цвета, spinner, TUI элементы
- ❌ Визуальный мусор засоряет логи
- ❌ Сложность парсинга stdout (регексы, хрупкость)
- ❌ Каждое обновление Claude CLI может сломать парсинг

### Целевая Архитектура (Headless Mode)

```
Telegram Bot → workers-spawner.send_message(agent_id, message)
                    ↓
            AgentFactory.send_message_headless()
                    ↓
            docker exec -i claude -p "..." --output-format json --resume session_id
                    ↓
            Чистый JSON: {"result": "...", "session_id": "..."}
                    ↓
            Telegram Bot (прямой ответ)
```

**Преимущества**:
- ✅ Чистый JSON вывод без визуального мусора
- ✅ Нативная поддержка session management через `--resume`
- ✅ Простой парсинг (json.loads)
- ✅ Стабильность (официальный API Claude CLI)
- ✅ Универсальность (аналогично для Factory.ai Droid)

---

## Ключевые Принципы Миграции

### 1. Сохранение Auth Механизма

**Текущий подход сохраняется без изменений**:
- `mount_session_volume=True`: Маунтит `HOST_CLAUDE_DIR` (например `~/.claude`) в контейнер
- OAuth credentials из `~/.claude/.credentials.json` используются автоматически
- API key НЕ используется (приоритет у OAuth)

**Важно**: container_service.py:99-112 логика остаётся:
```python
# Skip ANTHROPIC_API_KEY if mounting session (OAuth takes precedence)
if required_var == "ANTHROPIC_API_KEY" and config.mount_session_volume:
    logger.info("skipping_api_key_for_session",
                reason="Using OAuth session from mounted volume")
    continue
```

### 2. Полиморфизм

**Никаких Claude-специфичных деталей на верхних уровнях**:
- redis_handlers.py → агностичен к агенту
- telegram bot → агностичен к агенту
- Вся Claude-логика внутри ClaudeCodeAgent.send_message_headless()

### 3. Беспощадное Удаление Legacy

**Удаляем без сожалений**:
- ❌ process_manager.py (весь файл)
- ❌ log_collector.py (весь файл)
- ❌ ResponseListener в telegram bot
- ❌ send_message_persistent из redis_handlers
- ❌ persistent=True флаг из create_agent
- ❌ Все ссылки на PTY/pexpect

---

## Итеративный План Выполнения

### Фаза 0: Подготовка (30 мин)

**Задачи**:
- [x] Изучить текущую архитектуру
- [x] Составить этот план
- [ ] Review плана
- [ ] Создать feature branch `refactor/headless-mode`

**Критерий завершения**: План утверждён, ветка создана

---

### Фаза 1: Реализация Headless Mode в AgentFactory (2 часа)

#### Шаг 1.1: Обновить ClaudeCodeAgent

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/claude_code.py`

**Удалить**:
- `get_persistent_command()` - больше не нужен
- `format_message_for_stdin()` - больше не нужен

**Добавить**:
```python
import json
import shlex
from typing import Any

async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
) -> dict[str, Any]:
    """Send message using headless mode with clean JSON output.

    Uses claude -p with --output-format json for structured response.
    Session continuity via --resume session_id.

    Args:
        agent_id: Container ID
        message: User message text
        session_context: Optional session state (contains session_id)

    Returns:
        {
            "response": str,  # Agent's text response
            "session_context": dict,  # Updated session (session_id)
            "metadata": dict,  # Usage stats, model info
        }
    """
    session_id = session_context.get("session_id") if session_context else None

    # Build command with proper escaping
    # Use shlex.quote for safety
    cmd_parts = [
        "claude",
        "-p", shlex.quote(message),
        "--output-format", "json",
        "--dangerously-skip-permissions",
    ]

    if session_id:
        cmd_parts.extend(["--resume", session_id])

    full_command = " ".join(cmd_parts)

    logger.info(
        "sending_headless_message",
        agent_id=agent_id,
        has_session=bool(session_id),
        message_length=len(message),
    )

    # Execute via ContainerService.send_command
    result = await self.container_service.send_command(
        agent_id,
        full_command,
        timeout=120
    )

    if result.exit_code != 0:
        logger.error(
            "headless_command_failed",
            agent_id=agent_id,
            exit_code=result.exit_code,
            error=result.error,
        )
        raise RuntimeError(f"Claude CLI failed: {result.error}")

    # Parse JSON response
    try:
        data = json.loads(result.output)

        return {
            "response": data["result"],
            "session_context": {"session_id": data["session_id"]},
            "metadata": {
                "usage": data.get("usage", {}),
                "model": data.get("model"),
            }
        }
    except json.JSONDecodeError as e:
        logger.error(
            "failed_to_parse_json",
            agent_id=agent_id,
            output_preview=result.output[:500],
            error=str(e),
        )
        raise RuntimeError(f"Failed to parse Claude response: {e}")
```

**Критерий завершения**: ClaudeCodeAgent.send_message_headless() реализован

---

#### Шаг 1.2: Обновить FactoryDroidAgent (заглушка)

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/factory_droid.py`

**Удалить**:
- `get_persistent_command()`
- `format_message_for_stdin()`

**Добавить**:
```python
async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
) -> dict[str, Any]:
    """Send message to Factory Droid using headless exec mode.

    Note: Factory Droid has different session management.
    Context is handled via workspace state, not session_id.
    """
    import shlex

    cmd = f"/home/worker/.local/bin/droid exec -o json {shlex.quote(message)}"

    result = await self.container_service.send_command(
        agent_id,
        cmd,
        timeout=120
    )

    if result.exit_code != 0:
        raise RuntimeError(f"Droid exec failed: {result.error}")

    # Parse output (droid exec format may vary)
    try:
        data = json.loads(result.output)
        return {
            "response": data.get("result", result.output),
            "session_context": session_context,  # Preserve as-is
            "metadata": {}
        }
    except json.JSONDecodeError:
        # Fallback: treat output as plain text
        return {
            "response": result.output,
            "session_context": session_context,
            "metadata": {}
        }
```

**Критерий завершения**: FactoryDroidAgent.send_message_headless() реализован (заглушка)

---

#### Шаг 1.3: Обновить базовый класс AgentFactory

**Файл**: `services/workers-spawner/src/workers_spawner/factories/base.py`

**Удалить**:
```python
@abstractmethod
def get_persistent_command(self) -> str:
    ...

@abstractmethod
def format_message_for_stdin(self, message: str) -> str:
    ...
```

**Добавить**:
```python
@abstractmethod
async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
) -> dict[str, Any]:
    """Send message to agent in headless mode.

    This is the universal interface for all CLI agents.
    Each agent implements its own protocol (claude -p, droid exec, etc.)

    Args:
        agent_id: Container ID
        message: User message text
        session_context: Optional agent-specific session state

    Returns:
        {
            "response": str,  # Agent's text response
            "session_context": dict | None,  # Updated session state
            "metadata": dict,  # Agent-specific metadata
        }
    """
```

**Критерий завершения**: Интерфейс обновлён, все агенты реализуют send_message_headless()

---

### Фаза 2: Обновление workers-spawner Redis Handlers (1.5 часа)

#### Шаг 2.1: Удалить Persistent Mode Infrastructure

**Файлы для удаления**:
1. `services/workers-spawner/src/workers_spawner/process_manager.py` - **УДАЛИТЬ**
2. `services/workers-spawner/src/workers_spawner/log_collector.py` - **УДАЛИТЬ**

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Удалить**:
- `_handle_send_message_persistent()` - больше не нужен
- Импорты `ProcessManager` и `LogCollector`
- Поля `self.process_manager` и `self.log_collector` из `__init__`
- Логику запуска persistent process в `_handle_create`
- "send_message_persistent" из `self._handlers`

**Критерий завершения**: Persistent mode код удалён полностью

---

#### Шаг 2.2: Добавить Handler для Headless Mode

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Обновить `_handlers` dict**:
```python
self._handlers = {
    "create": self._handle_create,
    "send_command": self._handle_send_command,
    "send_message": self._handle_send_message,  # ← НОВЫЙ
    "send_file": self._handle_send_file,
    "status": self._handle_status,
    "logs": self._handle_logs,
    "delete": self._handle_delete,
}
```

**Добавить новый handler**:
```python
async def _handle_send_message(self, message: dict[str, Any]) -> dict[str, Any]:
    """Handle send_message command using headless mode.

    This is agent-agnostic - all Claude-specific logic is in ClaudeCodeAgent.

    Expected fields:
    - agent_id: str
    - message: str (user message text)

    Returns:
    - response: str (agent response text)
    - metadata: dict (optional agent metadata)
    """
    agent_id = message.get("agent_id")
    user_message = message.get("message")

    if not agent_id or not user_message:
        raise ValueError("Missing 'agent_id' or 'message' field")

    # Get container metadata
    metadata = self.containers._containers.get(agent_id)
    if not metadata:
        raise ValueError(f"Agent {agent_id} not found")

    config: WorkerConfig = metadata["config"]

    # Get factory for this agent type (polymorphic!)
    from workers_spawner.factories.registry import get_agent_factory
    factory = get_agent_factory(config.agent, self.containers)

    # Get session context from Redis (SessionManager already exists!)
    session_context = await self.containers.session_manager.get_session_context(agent_id)

    logger.info(
        "handling_send_message",
        agent_id=agent_id,
        agent_type=config.agent.value,
        has_session=bool(session_context),
    )

    # Send message via factory's headless method
    result = await factory.send_message_headless(
        agent_id=agent_id,
        message=user_message,
        session_context=session_context,
    )

    # Save updated session context
    new_context = result.get("session_context")
    if new_context:
        ttl = config.ttl_hours * 3600
        await self.containers.session_manager.save_session_context(
            agent_id, new_context, ttl_seconds=ttl
        )
        logger.debug("session_context_updated", agent_id=agent_id)

    return {
        "response": result["response"],
        "metadata": result.get("metadata", {}),
    }
```

**Критерий завершения**: send_message handler работает, агностичен к типу агента

---

#### Шаг 2.3: Упростить _handle_create

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Удалить из `_handle_create`**:
```python
# Весь блок persistent process (строки ~98-118)
# Start persistent process if requested...
# await self.process_manager.start_process(...)
# await self.log_collector.start_collecting(...)
```

**Убрать параметр**:
```python
# Было:
use_persistent = message.get("persistent", False)

# Теперь:
# (просто удалить эту строку и весь блок ниже)
```

**Критерий завершения**: _handle_create упрощён, persistent mode код удалён

---

#### Шаг 2.4: Упростить _handle_delete

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Удалить**:
```python
# Stop persistent process if running
if self.process_manager and self.process_manager.is_running(agent_id):
    await self.process_manager.stop_process(agent_id)

# Stop log collector
if self.log_collector:
    await self.log_collector.stop_collecting(agent_id)
```

**Критерий завершения**: _handle_delete упрощён

---

#### Шаг 2.5: Упростить CommandHandler.__init__

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Было**:
```python
def __init__(
    self,
    redis_client: redis.Redis,
    container_service: ContainerService,
    event_publisher: EventPublisher,
    process_manager: ProcessManager | None = None,
    log_collector: LogCollector | None = None,
):
    ...
    self.process_manager = process_manager
    self.log_collector = log_collector
```

**Теперь**:
```python
def __init__(
    self,
    redis_client: redis.Redis,
    container_service: ContainerService,
    event_publisher: EventPublisher,
):
    # process_manager и log_collector удалены
    self.redis = redis_client
    self.containers = container_service
    self.events = event_publisher
    self.settings = get_settings()
```

**Критерий завершения**: Инициализация упрощена

---

### Фаза 3: Обновление Telegram Bot (1 час)

#### Шаг 3.1: Обновить WorkersSpawnerClient

**Файл**: `services/telegram_bot/src/clients/workers_spawner.py`

**Удалить**:
```python
async def send_message_persistent(self, agent_id: str, message: str) -> dict:
    # Весь метод - УДАЛИТЬ
```

**Добавить**:
```python
async def send_message(
    self,
    agent_id: str,
    message: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """Send text message to agent (headless mode).

    High-level API for agent communication.
    Returns response synchronously (not via Redis stream).

    Args:
        agent_id: Agent container ID
        message: User message text
        timeout: Request timeout in seconds

    Returns:
        {
            "response": str,  # Agent's response
            "metadata": dict,  # Optional metadata
        }
    """
    response = await self._request(
        "send_message",
        {"agent_id": agent_id, "message": message},
        timeout=float(timeout + 5),
    )

    if not response.get("success", False):
        raise RuntimeError(f"send_message failed: {response.get('error')}")

    return {
        "response": response["response"],
        "metadata": response.get("metadata", {}),
    }
```

**Обновить create_agent**:
```python
# Было:
async def create_agent(self, user_id: str, mount_session_volume: bool = False, persistent: bool = False):
    ...
    config_dict = {
        ...
        "mount_session_volume": mount_session_volume,
    }

    await self._request(
        "create",
        {
            "config": config_dict,
            "context": {"user_id": user_id},
            "persistent": persistent,  # ← УДАЛИТЬ
        },
    )

# Теперь:
async def create_agent(self, user_id: str, mount_session_volume: bool = False):
    ...
    await self._request(
        "create",
        {
            "config": config_dict,
            "context": {"user_id": user_id},
            # persistent параметр удалён
        },
    )
```

**Критерий завершения**: Клиент обновлён, persistent параметр удалён

---

#### Шаг 3.2: Обновить AgentManager

**Файл**: `services/telegram_bot/src/agent_manager.py`

**Полностью переписать send_message**:
```python
async def send_message(self, user_id: int, message: str) -> str:
    """Send a message to the user's agent and return response.

    This is now synchronous - response is returned directly,
    not via Redis stream (headless mode).

    Args:
        user_id: Telegram user ID
        message: User message text

    Returns:
        Agent's response text
    """
    agent_id = await self.get_or_create_agent(user_id)

    logger.info("sending_message_headless", user_id=user_id, agent_id=agent_id)

    try:
        result = await workers_spawner.send_message(agent_id, message, timeout=120)

        logger.info(
            "message_sent_and_received",
            user_id=user_id,
            agent_id=agent_id,
            response_length=len(result["response"]),
        )

        return result["response"]

    except Exception as e:
        logger.error("send_message_failed", user_id=user_id, agent_id=agent_id, error=str(e))
        raise
```

**Обновить get_or_create_agent**:
```python
# Было:
agent_id = await workers_spawner.create_agent(
    str(user_id), mount_session_volume=mount_volume, persistent=True
)

# Теперь:
agent_id = await workers_spawner.create_agent(
    str(user_id), mount_session_volume=mount_volume
    # persistent=True удалён
)
```

**Критерий завершения**: AgentManager.send_message возвращает ответ синхронно

---

#### Шаг 3.3: Удалить ResponseListener

**Файлы для удаления**:
1. `services/telegram_bot/src/response_listener.py` - **УДАЛИТЬ ЦЕЛИКОМ**

**Файл**: `services/telegram_bot/src/main.py`

**Удалить**:
```python
# Импорт
from src.response_listener import response_listener

# В main():
# Запуск listener
listener_task = asyncio.create_task(response_listener.start(bot))

# В shutdown:
await response_listener.stop()
await response_listener.close()
```

**Критерий завершения**: ResponseListener удалён полностью

---

#### Шаг 3.4: Обновить Handlers

**Файл**: `services/telegram_bot/src/handlers.py`

**Найти обработчик сообщений и обновить**:
```python
# Было:
async def handle_message(update: Update, context) -> None:
    ...
    # Fire-and-forget
    await agent_manager.send_message(user_id, text)
    # Ответ придёт через ResponseListener

# Теперь:
async def handle_message(update: Update, context) -> None:
    ...
    # Synchronous request-response
    response_text = await agent_manager.send_message(user_id, text)

    # Send response immediately
    try:
        await update.message.reply_text(
            response_text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        # Fallback to plain text if markdown fails
        await update.message.reply_text(response_text)
```

**Критерий завершения**: Handler обновлён для синхронных ответов

---

### Фаза 4: Обновление Main Entrypoint (30 мин)

#### Шаг 4.1: Обновить workers-spawner main.py

**Файл**: `services/workers-spawner/src/workers_spawner/main.py`

**Удалить**:
```python
# Импорты
from workers_spawner.process_manager import ProcessManager
from workers_spawner.log_collector import LogCollector

# Инициализацию
process_manager = ProcessManager()
log_collector = LogCollector(redis_client)

# Передачу в CommandHandler
handler = CommandHandler(
    redis_client=redis_client,
    container_service=containers,
    event_publisher=events,
    process_manager=process_manager,  # ← УДАЛИТЬ
    log_collector=log_collector,  # ← УДАЛИТЬ
)
```

**Теперь**:
```python
handler = CommandHandler(
    redis_client=redis_client,
    container_service=containers,
    event_publisher=events,
)
```

**Критерий завершения**: Entrypoint упрощён

---

### Фаза 5: Тестирование (1.5 часа)

#### Шаг 5.1: Базовые Unit Тесты

**Создать**: `services/workers-spawner/tests/unit/test_headless_mode.py`

```python
"""Tests for headless mode implementation."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from workers_spawner.factories.agents.claude_code import ClaudeCodeAgent
from workers_spawner.container_service import ExecutionResult


@pytest.mark.asyncio
async def test_claude_headless_first_message():
    """Test sending first message without session_id."""
    factory = ClaudeCodeAgent(MagicMock())
    factory.container_service.send_command = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            output=json.dumps({
                "result": "Ёлка обычно зелёного цвета.",
                "session_id": "test-session-123",
                "model": "claude-sonnet-4-5",
            }),
            exit_code=0,
        )
    )

    result = await factory.send_message_headless(
        agent_id="test-agent",
        message="Какого цвета ёлка?",
        session_context=None,
    )

    assert result["response"] == "Ёлка обычно зелёного цвета."
    assert result["session_context"]["session_id"] == "test-session-123"


@pytest.mark.asyncio
async def test_claude_headless_with_session():
    """Test sending message with existing session_id."""
    factory = ClaudeCodeAgent(MagicMock())
    factory.container_service.send_command = AsyncMock(
        return_value=ExecutionResult(
            success=True,
            output=json.dumps({
                "result": "Предыдущий вопрос был о цвете ёлки.",
                "session_id": "test-session-123",
            }),
            exit_code=0,
        )
    )

    result = await factory.send_message_headless(
        agent_id="test-agent",
        message="Какой был предыдущий вопрос?",
        session_context={"session_id": "test-session-123"},
    )

    assert "предыдущий вопрос" in result["response"].lower()

    # Verify --resume was used
    call_args = factory.container_service.send_command.call_args
    command = call_args[0][1]
    assert "--resume test-session-123" in command
```

**Критерий завершения**: Unit тесты проходят

---

#### Шаг 5.2: Интеграционный Тест (Обязательный!)

**Создать**: `services/workers-spawner/tests/integration/test_headless_integration.py`

```python
"""Integration test for headless mode with real container.

This test MUST pass before considering migration complete.
"""
import pytest
import asyncio

from workers_spawner.container_service import ContainerService
from workers_spawner.models import WorkerConfig, AgentType, ToolGroup
from workers_spawner.factories.registry import get_agent_factory
from workers_spawner.session_manager import AgentSessionManager
import redis.asyncio as redis


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_headless_workflow():
    """Test complete headless workflow with real Claude container.

    Requirements:
    1. Simple question: "Какого цвета ёлка?"
    2. Memory test: "Какой был предыдущий вопрос?"
    3. Tool usage: Call orchestrator CLI endpoint
    """
    # Setup
    containers = ContainerService()
    redis_client = redis.from_url(containers.settings.redis_url, decode_responses=True)
    session_manager = AgentSessionManager(redis_client)

    config = WorkerConfig(
        name="test-headless",
        agent=AgentType.CLAUDE_CODE,
        capabilities=[],
        allowed_tools=[ToolGroup.ORCHESTRATOR_CLI],
        mount_session_volume=True,  # Use OAuth
    )

    # Create container
    agent_id = await containers.create_container(config, {"test": "true"})
    print(f"Created agent: {agent_id}")

    try:
        factory = get_agent_factory(AgentType.CLAUDE_CODE, containers)

        # Test 1: Simple question
        print("\n=== Test 1: Simple Question ===")
        result1 = await factory.send_message_headless(
            agent_id=agent_id,
            message="Какого цвета ёлка?",
            session_context=None,
        )

        print(f"Response: {result1['response']}")
        assert len(result1["response"]) > 0
        assert result1["session_context"]["session_id"]

        session_id = result1["session_context"]["session_id"]
        await session_manager.save_session_context(agent_id, result1["session_context"])

        # Test 2: Memory check
        print("\n=== Test 2: Memory Check ===")
        session_context = await session_manager.get_session_context(agent_id)

        result2 = await factory.send_message_headless(
            agent_id=agent_id,
            message="Какой был предыдущий вопрос?",
            session_context=session_context,
        )

        print(f"Response: {result2['response']}")
        # Must mention елка or цвет
        response_lower = result2["response"].lower()
        assert "ёлк" in response_lower or "цвет" in response_lower, \
            f"Agent doesn't remember previous question! Response: {result2['response']}"

        await session_manager.save_session_context(agent_id, result2["session_context"])

        # Test 3: Tool usage (orchestrator CLI)
        print("\n=== Test 3: Tool Usage ===")
        session_context = await session_manager.get_session_context(agent_id)

        result3 = await factory.send_message_headless(
            agent_id=agent_id,
            message=(
                "Используй orchestrator CLI чтобы вызвать команду 'orchestrator test-connection'. "
                "Покажи мне результат выполнения."
            ),
            session_context=session_context,
        )

        print(f"Response: {result3['response']}")
        # Should show command execution
        assert "orchestrator" in result3["response"].lower() or \
               "test-connection" in result3["response"].lower(), \
            f"Agent didn't use orchestrator CLI! Response: {result3['response']}"

        print("\n✅ All tests passed!")

    finally:
        # Cleanup
        await containers.delete(agent_id)
        await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(test_full_headless_workflow())
```

**Как запустить**:
```bash
# В tooling контейнере
make shell
cd /app/services/workers-spawner

# Убедиться что HOST_CLAUDE_DIR прокинут в .env
# Запустить тест
pytest tests/integration/test_headless_integration.py -v -s
```

**Критерий завершения**:
- ✅ Простой вопрос получил ответ
- ✅ Агент помнит предыдущий вопрос
- ✅ Агент использовал orchestrator CLI тулзы

**ВАЖНО**: Эта фаза БЛОКИРУЮЩАЯ. Без прохождения всех 3 требований миграция НЕ завершена.

---

#### Шаг 5.3: E2E Тест через Telegram

**Ручное тестирование**:
1. Запустить все сервисы: `make up`
2. Отправить боту: "Какого цвета ёлка?"
3. Проверить ответ пришёл
4. Отправить: "Какой был предыдущий вопрос?"
5. Убедиться что агент помнит

**Критерий завершения**: E2E workflow работает

---

### Фаза 6: Cleanup и Документация (30 мин)

#### Шаг 6.1: Удалить Старые Тесты

**Найти и удалить**:
```bash
# Найти тесты для persistent mode
grep -r "ProcessManager\|LogCollector\|persistent" services/workers-spawner/tests/

# Удалить устаревшие тесты
rm services/workers-spawner/tests/unit/test_process_manager.py
rm services/workers-spawner/tests/unit/test_log_collector.py
# И любые другие тесты persistent mode
```

**Критерий завершения**: Устаревшие тесты удалены

---

#### Шаг 6.2: Обновить Документацию

**Файл**: `CLAUDE.md` (project instructions)

**Обновить секцию про CLI agents**:
```markdown
## CLI Agents Architecture

CLI agents (Claude Code, Factory.ai Droid, etc.) run in Docker containers
and communicate via **headless mode** for clean JSON output.

### How it Works

1. **Container Creation**: Universal worker image with agent CLI installed
2. **Message Sending**: One-shot execution via `claude -p "..." --output-format json`
3. **Session Management**: Redis-based session context (e.g., `session_id` for Claude)
4. **Response**: Structured JSON parsed and returned directly

### Adding New Agents

See `services/workers-spawner/src/workers_spawner/factories/agents/` for examples.

Each agent must implement:
- `send_message_headless()` - core communication method
- `get_install_commands()` - CLI installation
- `generate_instructions()` - agent-specific config (CLAUDE.md, AGENTS.md)
```

**Обновить**: `docs/refactoring/agent-abstraction-refactoring.md`

Добавить в конец:
```markdown
## Update: Headless Mode Migration (2026-01-08)

The system was migrated from persistent PTY mode to headless one-shot mode.

**Changes**:
- ❌ Removed: ProcessManager, LogCollector, ResponseListener
- ✅ Added: `send_message_headless()` in AgentFactory
- ✅ Cleaner: JSON output, no visual noise
- ✅ Simpler: Direct request-response, no Redis stream

See `docs/refactoring/headless-mode-migration.md` for full details.
```

**Критерий завершения**: Документация обновлена

---

#### Шаг 6.3: Git Commit

```bash
git add .
git commit -m "refactor(workers-spawner): migrate to headless mode for clean JSON output

- Remove ProcessManager and LogCollector (PTY mode)
- Implement send_message_headless() in AgentFactory
- Update Redis handlers to use headless one-shot commands
- Simplify Telegram bot (direct responses, no ResponseListener)
- Add integration tests for session continuity and tool usage

BREAKING CHANGE: persistent mode removed, all agents use headless mode

Closes #[issue-number]
"
```

---

## Оценка Трудозатрат

| Фаза | Задач | Оценка | Накопительно |
|------|-------|--------|--------------|
| 0. Подготовка | 1 | 30 мин | 30 мин |
| 1. AgentFactory | 3 | 2 часа | 2.5 часа |
| 2. Redis Handlers | 5 | 1.5 часа | 4 часа |
| 3. Telegram Bot | 4 | 1 час | 5 часов |
| 4. Main Entrypoint | 1 | 30 мин | 5.5 часов |
| 5. Тестирование | 3 | 1.5 часа | 7 часов |
| 6. Cleanup | 3 | 30 мин | 7.5 часов |
| **ИТОГО** | **20** | **~8 часов** | - |

**Реалистично**: 1 полный рабочий день

---

## Критерии Успеха

- ✅ Никаких PTY/pexpect упоминаний в коде
- ✅ ProcessManager и LogCollector удалены
- ✅ ResponseListener удалён
- ✅ Все тесты проходят (включая интеграционный)
- ✅ Интеграционный тест показывает:
  - Простой вопрос работает
  - Память между запросами работает
  - Tool usage (orchestrator CLI) работает
- ✅ E2E через Telegram работает
- ✅ Никаких Claude-специфичных деталей на верхних уровнях
- ✅ Легко добавить новый агент (только новый файл в factories/agents/)

---

## Rollback Plan

Если что-то пойдёт не так:

```bash
# Откатить коммит
git revert HEAD

# Или просто переключиться на main
git checkout main

# Пересобрать сервисы
make build
make up
```

---

## Риски и Митигация

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Claude CLI меняет JSON формат | Низкая | Высокое | json.loads + try/except, fallback на plain text |
| Холодный старт медленнее persistent | Средняя | Среднее | Измерить latency, если критично → кэширование контейнеров |
| Потеря контекста между запросами | Низкая | Критическое | SessionManager + интеграционный тест #2 (обязательный!) |
| Session ID протухает | Низкая | Среднее | TTL = container TTL, пересоздать агента |

---

## Следующие Шаги После Миграции

1. Реализовать `FactoryDroidAgent.send_message_headless()` (полноценно)
2. Добавить метрики (latency, error rate)
3. Benchmarking: headless vs persistent (если нужно)
4. Structured output через `--json-schema` (опционально)
5. Streaming responses (если Claude CLI поддерживает в headless)

---

**Документ обновлён**: 2026-01-08
**Автор**: Claude Sonnet 4.5
**Статус**: Ready for implementation
