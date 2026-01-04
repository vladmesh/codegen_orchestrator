# Persistent Agents MVP Implementation Plan

**Цель**: Реализовать минимальную работающую версию оркестратора с persistent CLI-агентами, tool-based communication и proper session management.

**Статус**: Planning

**Дата создания**: 2026-01-04

---

## Проблемы текущей архитектуры

### ❌ Что не так сейчас:

1. **Ephemeral процессы**
   - Каждое сообщение = новый `claude -p "message"` процесс
   - Процесс умирает после ответа
   - История теряется между запусками

2. **Output-based communication**
   - Парсим JSON вывод Claude: `{"result": "...", "session_id": "..."}`
   - stdout агента = бизнес-логика (неправильно!)
   - Нет разделения между логами и ответами

3. **Session management complexity**
   - Храним session_id в Redis
   - Передаём `--resume session_id` каждый раз
   - При persistent процессе session_id не нужен

4. **Container readiness race condition**
   - Контейнер создаётся
   - Команда отправляется до завершения entrypoint
   - Exit code 127 "command not found"

### ✅ Целевая архитектура MVP:

1. **Persistent процессы**
   - Один процесс Claude на весь TTL контейнера (2 часа)
   - История сохраняется автоматически
   - Пишем в stdin, читаем stdout/stderr как логи

2. **Tool-based communication**
   - Агент отвечает через `orchestrator answer "текст"`
   - Workers-spawner перехватывает tool calls
   - stdout/stderr = логи для аналитики

3. **No session_id**
   - Один процесс = одна сессия
   - Контекст живёт пока контейнер жив

4. **Proper logging**
   - Все логи агента собираются
   - Доступны для PO через API

---

## MVP Scope

### Что делаем:

- ✅ Persistent процесс Claude в контейнере
- ✅ Stdin/stdout communication
- ✅ Tool `orchestrator answer` для ответов
- ✅ Tool `orchestrator ask` для вопросов (эскалация)
- ✅ Логирование stdout/stderr в Redis
- ✅ Один PO агент (Telegram → Claude)
- ✅ Graceful shutdown при TTL expiry

### Что НЕ делаем в MVP:

- ❌ BMAD-структура (Analyst, Engineering Lead, etc.)
- ❌ Субграфы (Engineering, DevOps)
- ❌ Agent-to-agent communication
- ❌ Streaming logs UI
- ❌ Context compaction
- ❌ Multiple agent types (только Claude пока)

---

## Архитектура MVP

### Компоненты:

```
┌─────────────────────────────────────────────────────┐
│ Telegram Bot                                        │
│ ┌─────────────────────────────────────────────────┐ │
│ │ agent_manager.py                                │ │
│ │ • get_or_create_agent(user_id)                  │ │
│ │ • send_message(user_id, text)                   │ │
│ │ • get_logs(user_id)  [NEW]                      │ │
│ └─────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │ Redis Streams
                   ▼
┌─────────────────────────────────────────────────────┐
│ Workers-Spawner                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ CommandHandler                                  │ │
│ │ • create → launch persistent process            │ │
│ │ • send_message → write to stdin                 │ │
│ │ • get_logs → read from Redis                    │ │
│ └──────────────────┬──────────────────────────────┘ │
│                    ▼                                │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ProcessManager  [NEW]                           │ │
│ │ • start_persistent_process(agent_id)            │ │
│ │ • write_to_stdin(agent_id, message)             │ │
│ │ • read_stdout_stream(agent_id)                  │ │
│ │ • monitor_health(agent_id)                      │ │
│ └──────────────────┬──────────────────────────────┘ │
│                    ▼                                │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ToolCallListener  [NEW]                         │ │
│ │ • listen_for_tool_calls(stdout)                 │ │
│ │ • execute_tool(tool_name, args)                 │ │
│ │ • return_result_to_stdin(result)                │ │
│ └─────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────┐
│ Docker Container (universal-worker)                 │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Persistent Process:                             │ │
│ │   claude --dangerously-skip-permissions         │ │
│ │                                                 │ │
│ │ stdin  ← messages from ProcessManager          │ │
│ │ stdout → logs + tool calls to ToolCallListener │ │
│ │ stderr → error logs                             │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

### Data Flow:

```
1. User sends "Создай проект X" via Telegram
   ↓
2. Telegram bot → Workers-Spawner: send_message(agent_id, "Создай проект X")
   ↓
3. ProcessManager.write_to_stdin(agent_id, "Создай проект X\n")
   ↓
4. Claude процесс получает в stdin, думает...
   ↓
5. Claude вызывает tool: orchestrator answer "Проект X создан!"
   ↓
6. ToolCallListener перехватывает из stdout
   ↓
7. Публикует в Redis: agent:{agent_id}:response → "Проект X создан!"
   ↓
8. Telegram bot получает через PubSub → отправляет юзеру
```

---

## Фаза 0: Подготовка и дизайн

**Цель**: Спроектировать интерфейсы и структуры данных.

### Задачи:

- [x] Анализ текущей архитектуры
- [x] Определение MVP scope
- [ ] Дизайн ProcessManager API
- [ ] Дизайн ToolCallListener протокола
- [ ] Дизайн логирования
- [ ] Review плана с командой

### Deliverables:

- Этот документ
- API спецификации (см. ниже)

---

## Фаза 1: ProcessManager - Persistent Process Lifecycle

**Цель**: Создать компонент для управления persistent процессами в контейнерах.

### Шаг 1.1: Создать ProcessManager

**Файл**: `services/workers-spawner/src/workers_spawner/process_manager.py` (новый)

**API:**

```python
class ProcessManager:
    """Manages persistent agent processes in containers."""

    def __init__(self):
        self._processes: dict[str, ProcessInfo] = {}  # agent_id → ProcessInfo

    async def start_process(
        self,
        agent_id: str,
        command: list[str],
        env: dict[str, str],
    ) -> None:
        """Start persistent process in container.

        Args:
            agent_id: Container ID
            command: Command to run (e.g., ["claude", "--dangerously-skip-permissions"])
            env: Environment variables

        Raises:
            RuntimeError: If process fails to start
        """

    async def write_to_stdin(
        self,
        agent_id: str,
        message: str,
    ) -> None:
        """Write message to process stdin.

        Args:
            agent_id: Container ID
            message: Text to send (will add newline)
        """

    async def read_stdout_line(
        self,
        agent_id: str,
        timeout: float = None,
    ) -> str | None:
        """Read one line from stdout (non-blocking).

        Returns:
            Line of text or None if no data available
        """

    async def get_process_status(
        self,
        agent_id: str,
    ) -> ProcessStatus:
        """Get process status.

        Returns:
            ProcessStatus with state, uptime, etc.
        """

    async def stop_process(
        self,
        agent_id: str,
        graceful: bool = True,
    ) -> None:
        """Stop persistent process.

        Args:
            graceful: If True, send SIGTERM then SIGKILL
        """

@dataclass
class ProcessInfo:
    """Information about running process."""
    agent_id: str
    container_id: str
    command: list[str]
    started_at: datetime
    stdin_pipe: asyncio.StreamWriter
    stdout_pipe: asyncio.StreamReader
    stderr_pipe: asyncio.StreamReader
    returncode: int | None = None
```

**Реализация:**

```python
import asyncio
from datetime import datetime, UTC

class ProcessManager:
    def __init__(self):
        self._processes: dict[str, ProcessInfo] = {}

    async def start_process(self, agent_id: str, command: list[str], env: dict[str, str]) -> None:
        """Start persistent process via docker exec."""

        # Build docker exec command
        docker_cmd = [
            "docker", "exec",
            "-i",  # Interactive (keep stdin open)
            agent_id,
            "/bin/bash", "-c",
            # Запускаем процесс в фоне, перенаправляем IO
            f"exec {' '.join(command)}",
        ]

        # Добавляем env vars
        env_str = " ".join([f"{k}={v}" for k, v in env.items()])
        if env_str:
            docker_cmd[-1] = f"{env_str} {docker_cmd[-1]}"

        # Запускаем процесс
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Сохраняем информацию
        self._processes[agent_id] = ProcessInfo(
            agent_id=agent_id,
            container_id=agent_id,
            command=command,
            started_at=datetime.now(UTC),
            stdin_pipe=proc.stdin,
            stdout_pipe=proc.stdout,
            stderr_pipe=proc.stderr,
        )

        logger.info(
            "persistent_process_started",
            agent_id=agent_id,
            command=" ".join(command),
        )

    async def write_to_stdin(self, agent_id: str, message: str) -> None:
        """Write to stdin."""
        process = self._processes.get(agent_id)
        if not process:
            raise ValueError(f"Process {agent_id} not found")

        # Добавляем newline если нет
        if not message.endswith("\n"):
            message += "\n"

        process.stdin_pipe.write(message.encode())
        await process.stdin_pipe.drain()

        logger.debug(
            "wrote_to_stdin",
            agent_id=agent_id,
            message_length=len(message),
        )
```

**Тесты**: `services/workers-spawner/tests/unit/test_process_manager.py`

- Mock docker exec
- Test start/stop/write
- Test process crash handling

**Критерий завершения**: ProcessManager запускает и управляет persistent процессом

---

### Шаг 1.2: Интегрировать ProcessManager в ContainerService

**Файл**: `services/workers-spawner/src/workers_spawner/container_service.py`

**Изменения:**

```python
from workers_spawner.process_manager import ProcessManager

class ContainerService:
    def __init__(self):
        # ... existing code ...
        self.process_manager = ProcessManager()

    async def create_container(self, config: WorkerConfig, context: dict) -> str:
        """Create container AND start persistent process."""

        # ... existing container creation code ...

        # Wait for container ready
        await self._wait_for_container_ready(agent_id)

        # Create setup files
        # ... existing code ...

        # NEW: Start persistent process
        agent_command = parser.get_agent_command()
        env_vars = parser.get_env_vars()

        await self.process_manager.start_process(
            agent_id=agent_id,
            command=[agent_command, "--dangerously-skip-permissions"],
            env=env_vars,
        )

        logger.info("container_created_with_process", agent_id=agent_id)
        return agent_id

    async def delete(self, agent_id: str) -> bool:
        """Stop process and delete container."""

        # NEW: Stop persistent process gracefully
        try:
            await self.process_manager.stop_process(agent_id, graceful=True)
        except Exception as e:
            logger.warning("process_stop_failed", agent_id=agent_id, error=str(e))

        # ... existing deletion code ...
```

**Критерий завершения**: При создании контейнера запускается persistent процесс

---

## Фаза 2: ToolCallListener - Перехват и выполнение тулзов

**Цель**: Слушать stdout процесса, перехватывать tool calls, выполнять их.

### Шаг 2.1: Определить протокол tool calls

**Формат вывода Claude Code:**

```
[Agent думает, выводит в stdout]
Thinking about the task...
Analyzing requirements...

<function_calls>
<invoke name="orchestrator">
<parameter name="command">answer</parameter>
<parameter name="message">Проект создан успешно!</parameter>
</invoke>
</function_calls>

[Ждёт результата]
```

**Или через bash wrapper:**

```bash
# В контейнере агента устанавливаем orchestrator CLI
orchestrator answer "Проект создан!"

# Который выводит в stdout:
__TOOL_CALL__:{"tool":"answer","args":{"message":"Проект создан!"}}
```

**Выбираем вариант 2** - bash wrapper проще парсить.

---

### Шаг 2.2: Создать orchestrator CLI tool

**Файл**: `services/universal-worker/orchestrator-cli/orchestrator` (bash скрипт)

```bash
#!/bin/bash
# Orchestrator CLI tool for agents

set -e

COMMAND=$1
shift

case "$COMMAND" in
    answer)
        MESSAGE="$1"
        echo "__TOOL_CALL__:{\"tool\":\"answer\",\"args\":{\"message\":\"$MESSAGE\"}}"
        # Ждём ответа от Workers-Spawner
        read -r RESULT
        echo "$RESULT"
        ;;

    ask)
        MESSAGE="$1"
        TO="${2:-$PARENT_AGENT_ID}"
        echo "__TOOL_CALL__:{\"tool\":\"ask\",\"args\":{\"message\":\"$MESSAGE\",\"to\":\"$TO\"}}"
        read -r RESULT
        echo "$RESULT"
        ;;

    project)
        SUBCOMMAND="$1"
        shift
        ARGS=$(jq -n --arg cmd "$SUBCOMMAND" --args '$ARGS' "$@")
        echo "__TOOL_CALL__:{\"tool\":\"project\",\"args\":$ARGS}"
        read -r RESULT
        echo "$RESULT"
        ;;

    *)
        echo "Unknown command: $COMMAND" >&2
        exit 1
        ;;
esac
```

**Установка в контейнере:**

```dockerfile
# services/universal-worker/Dockerfile
COPY orchestrator-cli/orchestrator /usr/local/bin/orchestrator
RUN chmod +x /usr/local/bin/orchestrator
```

**Критерий завершения**: Агент может вызвать `orchestrator answer "текст"`

---

### Шаг 2.3: Создать ToolCallListener

**Файл**: `services/workers-spawner/src/workers_spawner/tool_call_listener.py` (новый)

```python
import asyncio
import json
import re
from typing import Callable, Awaitable

TOOL_CALL_PATTERN = re.compile(r'__TOOL_CALL__:({.*})')

class ToolCallListener:
    """Listens to process stdout for tool calls and executes them."""

    def __init__(
        self,
        process_manager: ProcessManager,
        tool_executor: Callable[[str, dict], Awaitable[dict]],
    ):
        self.process_manager = process_manager
        self.tool_executor = tool_executor
        self._listeners: dict[str, asyncio.Task] = {}

    async def start_listening(self, agent_id: str) -> None:
        """Start listening to agent's stdout for tool calls."""
        task = asyncio.create_task(self._listen_loop(agent_id))
        self._listeners[agent_id] = task

    async def stop_listening(self, agent_id: str) -> None:
        """Stop listening."""
        task = self._listeners.pop(agent_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _listen_loop(self, agent_id: str) -> None:
        """Main listening loop."""
        logger.info("tool_call_listener_started", agent_id=agent_id)

        try:
            while True:
                # Read line from stdout
                line = await self.process_manager.read_stdout_line(agent_id, timeout=1.0)

                if not line:
                    await asyncio.sleep(0.1)
                    continue

                # Log to Redis (для аналитики)
                await self._log_stdout(agent_id, line)

                # Check for tool call
                match = TOOL_CALL_PATTERN.search(line)
                if match:
                    tool_call_json = match.group(1)
                    await self._handle_tool_call(agent_id, tool_call_json)

        except asyncio.CancelledError:
            logger.info("tool_call_listener_stopped", agent_id=agent_id)
            raise
        except Exception as e:
            logger.error("tool_call_listener_error", agent_id=agent_id, error=str(e))

    async def _handle_tool_call(self, agent_id: str, tool_call_json: str) -> None:
        """Parse and execute tool call."""
        try:
            tool_call = json.loads(tool_call_json)
            tool_name = tool_call["tool"]
            tool_args = tool_call["args"]

            logger.info(
                "tool_call_received",
                agent_id=agent_id,
                tool=tool_name,
                args=tool_args,
            )

            # Execute tool
            result = await self.tool_executor(tool_name, tool_args)

            # Return result to agent's stdin
            result_json = json.dumps(result)
            await self.process_manager.write_to_stdin(agent_id, result_json)

            logger.info(
                "tool_call_executed",
                agent_id=agent_id,
                tool=tool_name,
                success=result.get("success", False),
            )

        except Exception as e:
            logger.error(
                "tool_call_execution_failed",
                agent_id=agent_id,
                error=str(e),
            )
            # Return error to agent
            error_result = json.dumps({"success": False, "error": str(e)})
            await self.process_manager.write_to_stdin(agent_id, error_result)

    async def _log_stdout(self, agent_id: str, line: str) -> None:
        """Log stdout line to Redis."""
        # TODO: Implement in Phase 3
        pass
```

**Критерий завершения**: ToolCallListener перехватывает и выполняет tool calls

---

### Шаг 2.4: Создать ToolExecutor

**Файл**: `services/workers-spawner/src/workers_spawner/tool_executor.py` (новый)

```python
class ToolExecutor:
    """Executes tools called by agents."""

    def __init__(
        self,
        event_publisher: EventPublisher,
        api_client: OrchestratorAPIClient,  # NEW
    ):
        self.events = event_publisher
        self.api = api_client

        # Tool handlers
        self._handlers = {
            "answer": self._handle_answer,
            "ask": self._handle_ask,
            "project": self._handle_project,
            "deploy": self._handle_deploy,
            # ... more tools
        }

    async def execute(self, tool_name: str, args: dict) -> dict:
        """Execute tool and return result."""
        handler = self._handlers.get(tool_name)
        if not handler:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}",
            }

        try:
            return await handler(args)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    async def _handle_answer(self, args: dict) -> dict:
        """Handle 'orchestrator answer' tool.

        Publishes answer to response channel for Telegram bot.
        """
        message = args.get("message")
        agent_id = args.get("agent_id")  # Injected by ToolCallListener

        if not message:
            return {"success": False, "error": "Missing message"}

        # Publish to Redis for Telegram bot
        await self.events.publish_response(agent_id, message)

        logger.info("answer_published", agent_id=agent_id, message_length=len(message))

        return {
            "success": True,
            "message": "Answer published",
        }

    async def _handle_project(self, args: dict) -> dict:
        """Handle 'orchestrator project' tool.

        Proxies to Orchestrator API.
        """
        subcommand = args.get("subcommand")

        if subcommand == "create":
            result = await self.api.create_project(
                name=args.get("name"),
                description=args.get("description"),
            )
            return {
                "success": True,
                "project_id": result["id"],
                "message": f"Project created: {result['id']}",
            }

        elif subcommand == "list":
            projects = await self.api.list_projects()
            return {
                "success": True,
                "projects": projects,
            }

        # ... more subcommands
```

**Критерий завершения**: Tool executor выполняет базовые тулзы

---

## Фаза 3: Logging - Сбор и хранение логов агентов

**Цель**: Собирать все логи агентов (stdout/stderr) для аналитики и debugging.

### Шаг 3.1: Определить формат хранения

**Опции:**
1. Redis Streams (retention last 1000 lines)
2. PostgreSQL (structured logs)
3. File storage (S3/MinIO)

**Выбираем**: Redis Streams для MVP (простота + скорость)

**Schema:**

```python
# Redis Stream: agent:{agent_id}:logs
{
    "timestamp": "2026-01-04T12:34:56.789Z",
    "level": "info",  # info, error, debug
    "source": "stdout",  # stdout, stderr, system
    "message": "Processing request...",
    "agent_id": "agent-abc123",
}
```

---

### Шаг 3.2: Реализовать LogCollector

**Файл**: `services/workers-spawner/src/workers_spawner/log_collector.py` (новый)

```python
import asyncio
from datetime import datetime, UTC
import redis.asyncio as redis

class LogCollector:
    """Collects and stores agent logs."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self._collectors: dict[str, asyncio.Task] = {}

    async def start_collecting(
        self,
        agent_id: str,
        process_manager: ProcessManager,
    ) -> None:
        """Start collecting logs from agent."""
        # Start stdout collector
        stdout_task = asyncio.create_task(
            self._collect_stream(agent_id, process_manager, "stdout")
        )
        # Start stderr collector
        stderr_task = asyncio.create_task(
            self._collect_stream(agent_id, process_manager, "stderr")
        )

        self._collectors[agent_id] = asyncio.gather(stdout_task, stderr_task)

    async def stop_collecting(self, agent_id: str) -> None:
        """Stop collecting logs."""
        task = self._collectors.pop(agent_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _collect_stream(
        self,
        agent_id: str,
        process_manager: ProcessManager,
        stream_type: str,  # "stdout" or "stderr"
    ) -> None:
        """Collect lines from stdout or stderr."""
        logger.info("log_collector_started", agent_id=agent_id, stream=stream_type)

        try:
            while True:
                # Read line
                if stream_type == "stdout":
                    line = await process_manager.read_stdout_line(agent_id, timeout=1.0)
                else:
                    line = await process_manager.read_stderr_line(agent_id, timeout=1.0)

                if not line:
                    await asyncio.sleep(0.1)
                    continue

                # Store in Redis
                await self._store_log(agent_id, stream_type, line)

        except asyncio.CancelledError:
            logger.info("log_collector_stopped", agent_id=agent_id, stream=stream_type)
            raise
        except Exception as e:
            logger.error("log_collector_error", agent_id=agent_id, error=str(e))

    async def _store_log(
        self,
        agent_id: str,
        source: str,
        message: str,
    ) -> None:
        """Store log entry in Redis Stream."""
        stream_name = f"agent:{agent_id}:logs"

        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": "error" if source == "stderr" else "info",
            "source": source,
            "message": message,
            "agent_id": agent_id,
        }

        await self.redis.xadd(
            stream_name,
            entry,
            maxlen=1000,  # Keep last 1000 entries
        )

    async def get_logs(
        self,
        agent_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """Get recent logs for agent."""
        stream_name = f"agent:{agent_id}:logs"

        messages = await self.redis.xrevrange(
            stream_name,
            count=limit,
        )

        logs = []
        for msg_id, data in messages:
            logs.append({
                "id": msg_id,
                **data,
            })

        return logs
```

**Критерий завершения**: Логи агентов собираются в Redis

---

### Шаг 3.3: Добавить get_logs endpoint

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

```python
class CommandHandler:
    def __init__(self, ..., log_collector: LogCollector):
        # ... existing code ...
        self.log_collector = log_collector

        self._handlers["get_logs"] = self._handle_get_logs

    async def _handle_get_logs(self, message: dict) -> dict:
        """Handle get_logs command.

        Expected fields:
        - agent_id: str
        - limit: optional int (default 100)
        """
        agent_id = message.get("agent_id")
        limit = message.get("limit", 100)

        if not agent_id:
            raise ValueError("Missing 'agent_id' field")

        logs = await self.log_collector.get_logs(agent_id, limit)

        return {
            "logs": logs,
            "count": len(logs),
        }
```

**Критерий завершения**: Можно получить логи агента через API

---

## Фаза 4: Integration - Связываем всё вместе

**Цель**: Интегрировать ProcessManager, ToolCallListener, LogCollector в единую систему.

### Шаг 4.1: Обновить container_service.py

**Файл**: `services/workers-spawner/src/workers_spawner/container_service.py`

```python
from workers_spawner.process_manager import ProcessManager
from workers_spawner.tool_call_listener import ToolCallListener
from workers_spawner.log_collector import LogCollector
from workers_spawner.tool_executor import ToolExecutor

class ContainerService:
    def __init__(self):
        # ... existing code ...

        # NEW components
        self.process_manager = ProcessManager()
        self.log_collector = LogCollector(self.redis)
        self.tool_executor = ToolExecutor(
            event_publisher=EventPublisher(self.redis),
            api_client=OrchestratorAPIClient(),
        )
        self.tool_listener = ToolCallListener(
            process_manager=self.process_manager,
            tool_executor=self.tool_executor.execute,
        )

    async def create_container(self, config: WorkerConfig, context: dict) -> str:
        """Create container with persistent process."""

        # 1. Create Docker container
        agent_id = await self._create_docker_container(config, context)

        # 2. Wait for ready
        await self._wait_for_container_ready(agent_id)

        # 3. Setup files
        await self._setup_files(agent_id, config)

        # 4. Start persistent process
        await self.process_manager.start_process(
            agent_id=agent_id,
            command=["claude", "--dangerously-skip-permissions"],
            env=self._get_env_vars(config),
        )

        # 5. Start tool call listener
        await self.tool_listener.start_listening(agent_id)

        # 6. Start log collector
        await self.log_collector.start_collecting(agent_id, self.process_manager)

        logger.info("agent_fully_initialized", agent_id=agent_id)
        return agent_id

    async def delete(self, agent_id: str) -> bool:
        """Gracefully shutdown agent."""

        # 1. Stop log collector
        await self.log_collector.stop_collecting(agent_id)

        # 2. Stop tool listener
        await self.tool_listener.stop_listening(agent_id)

        # 3. Stop process
        await self.process_manager.stop_process(agent_id, graceful=True)

        # 4. Delete container
        await self._delete_docker_container(agent_id)

        # 5. Cleanup session
        await self.session_manager.delete_session_context(agent_id)

        logger.info("agent_deleted", agent_id=agent_id)
        return True
```

**Критерий завершения**: Полный lifecycle агента работает

---

### Шаг 4.2: Обновить send_message handler

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

```python
async def _handle_send_message(self, message: dict) -> dict:
    """Handle send_message - simplified for persistent agents.

    Expected fields:
    - agent_id: str
    - message: str

    Returns:
    - immediate acknowledgment
    - actual response comes via tool call (orchestrator answer)
    """
    agent_id = message.get("agent_id")
    user_message = message.get("message")

    if not agent_id or not user_message:
        raise ValueError("Missing required fields")

    # Write to stdin
    await self.containers.process_manager.write_to_stdin(
        agent_id,
        user_message,
    )

    logger.info(
        "message_sent_to_agent",
        agent_id=agent_id,
        message_length=len(user_message),
    )

    # Return immediately (response will come via tool call)
    return {
        "status": "sent",
        "message": "Message sent to agent, response will arrive via tool call",
    }
```

**ВАЖНО**: Теперь `send_message` не возвращает ответ сразу!

Ответ приходит через tool call:
```
Agent получает message в stdin
  ↓
Думает...
  ↓
Вызывает: orchestrator answer "Ответ"
  ↓
ToolCallListener перехватывает
  ↓
ToolExecutor.execute("answer", {...})
  ↓
Публикует в agents:{agent_id}:response
  ↓
Telegram bot получает через PubSub
```

**Критерий завершения**: Сообщения пишутся в stdin, ответы через tool calls

---

### Шаг 4.3: Обновить Telegram bot

**Файл**: `services/telegram_bot/src/agent_manager.py`

Нужно подписаться на `agents:{agent_id}:response` PubSub:

```python
class AgentManager:
    def __init__(self):
        # ... existing code ...
        self._response_listeners: dict[int, asyncio.Task] = {}

    async def start_response_listener(self, user_id: int, agent_id: str) -> None:
        """Start listening for responses from agent."""
        async def listen():
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(f"agents:{agent_id}:response")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    response_text = message["data"]
                    # Send to user via Telegram
                    await self._send_to_telegram(user_id, response_text)

        task = asyncio.create_task(listen())
        self._response_listeners[user_id] = task

    async def send_message(self, user_id: int, message: str) -> None:
        """Send message to agent (fire and forget)."""
        agent_id = await self.get_or_create_agent(user_id)

        # Start listener if not running
        if user_id not in self._response_listeners:
            await self.start_response_listener(user_id, agent_id)

        # Send message
        await workers_spawner.send_message(agent_id, message)

        logger.info("message_sent_to_agent", user_id=user_id, agent_id=agent_id)
```

**Альтернатива**: Использовать существующий EventPublisher.publish_response

**Критерий завершения**: Telegram bot получает ответы через PubSub

---

## Фаза 5: Testing & Stabilization

**Цель**: Протестировать MVP end-to-end и исправить баги.

### Шаг 5.1: Написать интеграционные тесты

**Файл**: `services/workers-spawner/tests/integration/test_persistent_agents.py`

```python
@pytest.mark.asyncio
async def test_persistent_agent_lifecycle():
    """Test full lifecycle: create → send message → get response → delete."""

    # 1. Create agent
    agent_id = await container_service.create_container(
        config=WorkerConfig(agent="claude-code", ...),
        context={"user_id": "test_user"},
    )

    # 2. Wait for initialization
    await asyncio.sleep(5)

    # 3. Send message
    await process_manager.write_to_stdin(agent_id, "Hello, what tools do I have?")

    # 4. Wait for tool call
    await asyncio.sleep(10)

    # 5. Check logs
    logs = await log_collector.get_logs(agent_id)
    assert len(logs) > 0

    # 6. Delete
    await container_service.delete(agent_id)

    # 7. Verify cleanup
    assert agent_id not in process_manager._processes


@pytest.mark.asyncio
async def test_tool_call_answer():
    """Test that 'orchestrator answer' tool publishes response."""

    # Setup
    agent_id = "test-agent"

    # Execute tool
    result = await tool_executor.execute("answer", {
        "agent_id": agent_id,
        "message": "Test response",
    })

    assert result["success"] is True

    # Verify published to Redis
    # ... check Redis PubSub
```

**Критерий завершения**: Интеграционные тесты проходят

---

### Шаг 5.2: Manual testing через Telegram

**Тест-кейсы:**

1. **Создание агента**
   - User: `/start`
   - Ожидаем: Контейнер создаётся, процесс запускается

2. **Простой вопрос**
   - User: "Привет!"
   - Ожидаем: Агент отвечает через `orchestrator answer`

3. **Tool usage**
   - User: "Создай проект TestProject"
   - Ожидаем: Агент вызывает `orchestrator project create`, получает project_id, отвечает

4. **Логи**
   - Admin: Запросить логи через API
   - Ожидаем: Видим stdout агента

5. **TTL expiry**
   - Подождать 2 часа
   - Ожидаем: Контейнер удаляется gracefully

6. **Multiple messages**
   - User: "Вопрос 1", "Вопрос 2", "Вопрос 3"
   - Ожидаем: История сохраняется, агент помнит контекст

**Критерий завершения**: Все тест-кейсы проходят

---

### Шаг 5.3: Performance & monitoring

**Метрики для мониторинга:**

1. **Process health**
   - Процесс жив?
   - Uptime
   - Memory usage

2. **Response time**
   - Время от send_message до answer
   - P50, P95, P99

3. **Tool call success rate**
   - Сколько tool calls успешных vs failed

4. **Log volume**
   - Сколько логов пишется в секунду
   - Нужно ли ротировать?

**Реализация:**

```python
# services/workers-spawner/src/workers_spawner/metrics.py

from prometheus_client import Counter, Histogram, Gauge

TOOL_CALLS_TOTAL = Counter(
    "agent_tool_calls_total",
    "Total tool calls by agents",
    ["tool_name", "status"],
)

RESPONSE_TIME = Histogram(
    "agent_response_time_seconds",
    "Time from message to answer",
)

ACTIVE_PROCESSES = Gauge(
    "agent_active_processes",
    "Number of active agent processes",
)
```

**Критерий завершения**: Метрики собираются, дашборды настроены

---

## Фаза 6: Documentation & Rollout

**Цель**: Задокументировать архитектуру и развернуть MVP.

### Шаг 6.1: Обновить документацию

**Файлы:**

1. `README.md` - добавить секцию про persistent agents
2. `ARCHITECTURE.md` - обновить диаграммы
3. `docs/TOOLS.md` - документировать orchestrator CLI tools
4. `docs/PERSISTENT_AGENTS.md` - гайд для разработчиков

**Критерий завершения**: Документация актуальна

---

### Шаг 6.2: Deploy MVP

**Чеклист:**

- [ ] Пересобрать образы (workers-spawner, universal-worker)
- [ ] Обновить переменные окружения
- [ ] Миграция существующих агентов (если есть)
- [ ] Запуск в production
- [ ] Мониторинг первые 24 часа

**Rollback plan:**

- Если критические баги → откатиться на предыдущую версию
- Persistent контейнеры удалить вручную

**Критерий завершения**: MVP работает в production

---

## Success Criteria MVP

Считаем MVP успешным если:

1. ✅ Persistent процесс Claude работает 2+ часа без падений
2. ✅ Tool `orchestrator answer` публикует ответы в Telegram
3. ✅ История сохраняется между сообщениями
4. ✅ Логи агентов доступны через API
5. ✅ Нет race conditions при создании контейнера
6. ✅ Graceful shutdown работает
7. ✅ Response time < 30 секунд (P95)
8. ✅ Uptime > 99% за неделю

---

## Risks & Mitigation

### Risk 1: Процесс Claude падает

**Mitigation:**
- Health check каждые 30 секунд
- Auto-restart при падении
- Сохранение snapshot контекста каждые 5 минут

### Risk 2: Context window exhaustion

**Mitigation:**
- Мониторинг token usage
- Alert при 150k tokens
- Manual `/compact` через orchestrator tool (в roadmap)

### Risk 3: stdout parsing проблемы

**Mitigation:**
- Использовать чёткий протокол `__TOOL_CALL__:{...}`
- Fallback на raw parsing если протокол сломан
- Тесты на различные форматы вывода

### Risk 4: Redis перегрузка логами

**Mitigation:**
- MAXLEN 1000 в streams
- TTL на log streams (24 часа)
- Опциональная архивация в S3 (roadmap)

---

## Timeline Estimate

| Фаза | Задачи | Время | Критичность |
|------|--------|-------|-------------|
| 0    | Design & Planning | 1 день | High |
| 1    | ProcessManager | 2-3 дня | Critical |
| 2    | ToolCallListener | 2-3 дня | Critical |
| 3    | Logging | 1-2 дня | Medium |
| 4    | Integration | 2-3 дня | Critical |
| 5    | Testing | 2-3 дня | High |
| 6    | Documentation & Rollout | 1 день | Medium |

**Total**: 11-16 дней

**Оптимизация**: Фазы 1-2 можно делать параллельно → 8-12 дней

---

## Next Steps

После завершения MVP:

1. См. `docs/bmad-roadmap.md` для дальнейшего развития
2. Собрать feedback от пользователей
3. Оптимизация производительности
4. Добавление новых tools
5. Расширение до BMAD-структуры

---

**Документ обновлён**: 2026-01-04
**Автор**: Claude Sonnet 4.5
**Статус**: Ready for Implementation
