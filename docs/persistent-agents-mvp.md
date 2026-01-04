# Persistent Agents MVP Implementation Plan

**Цель**: Реализовать универсальную систему persistent CLI-агентов с tool-based communication, поддерживающую любые типы агентов (Claude, Codex, Factory.ai, Gemini CLI и др.) через абстракцию и полиморфизм.

**Статус**: Planning

**Дата создания**: 2026-01-04
**Последнее обновление**: 2026-01-04

---

## Проблемы текущей архитектуры

### ❌ Что не так сейчас:

1. **Ephemeral процессы**
   - Каждое сообщение = новый `agent -p "message"` процесс
   - Процесс умирает после ответа
   - История теряется между запусками

2. **Output-based communication**
   - Парсим JSON вывод агента: `{"result": "...", "session_id": "..."}`
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

5. **Отсутствие абстракции**
   - Код завязан на конкретный CLI агент (Claude)
   - Нет полиморфизма для поддержки разных агентов
   - Детали реализации не инкапсулированы

### ✅ Целевая архитектура MVP:

1. **Persistent процессы**
   - Один процесс агента на весь TTL контейнера (2 часа)
   - История сохраняется автоматически
   - Пишем в stdin, читаем stdout/stderr как логи

2. **Tool-based communication**
   - Агент отвечает через tool calls: `orchestrator answer "текст"`
   - Workers-spawner перехватывает tool calls
   - stdout/stderr = логи для аналитики

3. **No session_id**
   - Один процесс = одна сессия
   - Контекст живёт пока контейнер жив

4. **Proper logging**
   - Все логи агента собираются
   - Доступны для PO через API

5. **Agent abstraction через фабрики**
   - Базовый интерфейс `AgentFactory`
   - Конкретные реализации: `ClaudeCodeAgent`, `FactoryDroidAgent`, `CodexAgent`, `GeminiCLIAgent`
   - Полиморфизм: `ProcessManager` работает с любым агентом
   - Инкапсуляция: детали реализации скрыты за интерфейсом

---

## MVP Scope

### Что делаем:

- ✅ **Agent abstraction** - универсальный интерфейс для всех CLI агентов
- ✅ **Factory pattern** - полиморфное создание агентов разных типов
- ✅ Persistent процесс агента в контейнере
- ✅ Stdin/stdout communication
- ✅ Tool `orchestrator answer` для ответов
- ✅ Tool `orchestrator ask` для вопросов (эскалация)
- ✅ Логирование stdout/stderr в Redis
- ✅ Один PO агент (Telegram → любой CLI агент)
- ✅ Graceful shutdown при TTL expiry
- ✅ Поддержка минимум 2 агентов из коробки: Claude Code и Factory Droid

### Что НЕ делаем в MVP:

- ❌ BMAD-структура (Analyst, Engineering Lead, etc.)
- ❌ Субграфы (Engineering, DevOps)
- ❌ Agent-to-agent communication
- ❌ Streaming logs UI
- ❌ Context compaction
- ❌ Поддержка всех возможных агентов (только 2-3 в MVP)

---

## Архитектура MVP

### Ключевой принцип: Абстракция через фабрики

Пользователь (Telegram Bot, API клиент) просто вызывает:
```python
await spawner.send_message(agent_id, "Hello")
```

Внутри workers-spawner:
```python
# Получаем фабрику для конкретного агента
factory = get_agent_factory(agent_type, container_service)

# ProcessManager работает с любым агентом через единый интерфейс
process_manager.write_to_stdin(agent_id, message)

# ToolCallListener настроен под конкретный агент
listener.pattern = factory.get_tool_call_pattern()
```

Детали реализации (как именно запускается Claude vs Factory vs Codex) скрыты за интерфейсом `AgentFactory`.

### Компоненты:

```
┌─────────────────────────────────────────────────────┐
│ Telegram Bot / API Client                          │
│ ┌─────────────────────────────────────────────────┐ │
│ │ agent_manager.py                                │ │
│ │ • get_or_create_agent(user_id, agent_type)      │ │
│ │ • send_message(user_id, text)  ← ЕДИНЫЙ МЕТОД   │ │
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
│ │ • delete → graceful shutdown                    │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ AgentFactory Registry (ПОЛИМОРФИЗМ)             │ │
│ │ ┌─────────────────────────────────────────────┐ │ │
│ │ │ AgentFactory (Abstract Base)                │ │ │
│ │ │ • start_persistent_process()                │ │ │
│ │ │ • write_to_stdin()                          │ │ │
│ │ │ • get_tool_call_pattern()                   │ │ │
│ │ │ • parse_tool_call()                         │ │ │
│ │ │ • generate_instructions()                   │ │ │
│ │ └─────────────────────────────────────────────┘ │ │
│ │ ┌─────────────────────────────────────────────┐ │ │
│ │ │ ClaudeCodeAgent extends AgentFactory        │ │ │
│ │ └─────────────────────────────────────────────┘ │ │
│ │ ┌─────────────────────────────────────────────┐ │ │
│ │ │ FactoryDroidAgent extends AgentFactory      │ │ │
│ │ └─────────────────────────────────────────────┘ │ │
│ │ ┌─────────────────────────────────────────────┐ │ │
│ │ │ CodexAgent extends AgentFactory             │ │ │
│ │ └─────────────────────────────────────────────┘ │ │
│ │ ┌─────────────────────────────────────────────┐ │ │
│ │ │ GeminiCLIAgent extends AgentFactory         │ │ │
│ │ └─────────────────────────────────────────────┘ │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ProcessManager (GENERIC)                        │ │
│ │ • start_process(agent_id, factory)              │ │
│ │ • write_to_stdin(agent_id, message)             │ │
│ │ • read_stdout_stream(agent_id)                  │ │
│ │ • stop_process(agent_id, graceful=True)         │ │
│ │ • _processes: {agent_id → {proc, factory}}      │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ ToolCallListener (AGENT-AWARE)                  │ │
│ │ • listen(agent_id, factory)  ← uses pattern     │ │
│ │ • parse_tool_call(line, factory)                │ │
│ │ • execute_tool(tool_name, args)                 │ │
│ │ • return_result_to_stdin(agent_id, result)      │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ LogCollector                                    │ │
│ │ • collect_stdout(agent_id, line)                │ │
│ │ • collect_stderr(agent_id, line)                │ │
│ │ • store_to_redis(agent_id, log_entry)           │ │
│ └─────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │ Docker exec / Process spawn
                   ▼
┌─────────────────────────────────────────────────────┐
│ Docker Container (universal-worker)                 │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Persistent Agent Process                        │ │
│ │ • Claude Code CLI (if agent_type=claude-code)   │ │
│ │ • Factory Droid (if agent_type=factory-droid)   │ │
│ │ • Codex CLI (if agent_type=codex)               │ │
│ │ • Gemini CLI (if agent_type=gemini-cli)         │ │
│ │                                                 │ │
│ │ stdin ←─ messages from ProcessManager           │ │
│ │ stdout → logs + tool calls                      │ │
│ │ stderr → error logs                             │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Orchestrator CLI Tools (Bash wrappers)          │ │
│ │ • orchestrator answer "text"                    │ │
│ │ • orchestrator ask "question"                   │ │
│ │ • orchestrator project                          │ │
│ │ • orchestrator deploy                           │ │
│ │ • orchestrator engineering                      │ │
│ │ • orchestrator infra                            │ │
│ │                                                 │ │
│ │ Each outputs: __TOOL_CALL__:{"tool":"..."}      │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

### Tool Call Protocol

Все агенты должны использовать единый формат tool calls в stdout:

```
__TOOL_CALL__:{"tool": "answer", "args": {"message": "Hello user!"}}
__TOOL_CALL__:{"tool": "ask", "args": {"question": "Which database?"}}
__TOOL_CALL__:{"tool": "project", "args": {"method": "create", "name": "myapp"}}
```

**Важно**: Формат единый, но способ генерации различается:
- **Claude Code**: использует bash wrappers `orchestrator answer "..."`
- **Factory Droid**: может использовать другие wrappers или напрямую выводить JSON
- **Codex/Gemini**: свои способы генерации tool calls

Фабрика каждого агента знает:
1. Какой паттерн искать в stdout (`get_tool_call_pattern()`)
2. Как парсить найденный tool call (`parse_tool_call()`)

---

## Детальный дизайн компонентов

### 1. AgentFactory (Расширение базового класса)

**Новые методы для persistent процессов:**

```python
class AgentFactory(ABC):
    """Abstract factory for CLI agents."""

    # === Existing methods ===
    @abstractmethod
    def get_install_commands(self) -> list[str]:
        """Install commands for agent."""

    @abstractmethod
    def get_agent_command(self) -> str:
        """Command to start agent (ephemeral)."""

    @abstractmethod
    def get_required_env_vars(self) -> list[str]:
        """Required environment variables."""

    @abstractmethod
    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate instruction files (CLAUDE.md, AGENTS.md, etc.)."""

    # === NEW: Persistent process support ===

    @abstractmethod
    def get_persistent_command(self) -> str:
        """Get command to start agent in persistent interactive mode.

        Examples:
            Claude: "claude --dangerously-skip-permissions"
            Factory: "droid --interactive"
            Codex: "codex --stdin"
            Gemini: "gemini-cli --chat-mode"

        Returns:
            Command string for persistent process.
        """

    @abstractmethod
    def get_tool_call_pattern(self) -> str:
        """Get regex pattern for detecting tool calls in stdout.

        Returns:
            Regex pattern string. Must have named group 'payload' for JSON.

        Example:
            r'__TOOL_CALL__:(?P<payload>\{.*\})'
        """

    @abstractmethod
    def parse_tool_call(self, match: re.Match) -> dict:
        """Parse matched tool call into structured format.

        Args:
            match: Regex match object from get_tool_call_pattern()

        Returns:
            {
                "tool": str,      # Tool name (answer, ask, project, etc.)
                "args": dict,     # Tool arguments
                "raw": str        # Raw matched string for debugging
            }
        """

    @abstractmethod
    def format_message_for_stdin(self, message: str) -> str:
        """Format user message for writing to agent's stdin.

        Some agents may need special formatting (e.g., JSON envelope).

        Args:
            message: Raw user message text

        Returns:
            Formatted message ready for stdin (with \n if needed)
        """

    def get_startup_timeout(self) -> int:
        """Timeout for agent startup in seconds.

        Override if agent needs longer startup time.

        Returns:
            Timeout in seconds (default: 30)
        """
        return 30

    def get_readiness_check(self) -> str | None:
        """Command to check if agent is ready.

        Returns None if no special check needed (default behavior).

        Returns:
            Command string to execute, or None
        """
        return None
```

### 2. Конкретные реализации фабрик

#### ClaudeCodeAgent

```python
@register_agent(AgentType.CLAUDE_CODE)
class ClaudeCodeAgent(AgentFactory):
    """Factory for Anthropic Claude Code CLI agent."""

    def get_persistent_command(self) -> str:
        """Claude в interactive режиме."""
        return "claude --dangerously-skip-permissions"

    def get_tool_call_pattern(self) -> str:
        """Claude uses bash wrappers that output __TOOL_CALL__:."""
        return r'__TOOL_CALL__:(?P<payload>\{.*\})'

    def parse_tool_call(self, match: re.Match) -> dict:
        """Parse JSON payload from Claude's tool calls."""
        import json
        payload = match.group('payload')
        data = json.loads(payload)
        return {
            "tool": data["tool"],
            "args": data["args"],
            "raw": match.group(0)
        }

    def format_message_for_stdin(self, message: str) -> str:
        """Claude expects plain text with newline."""
        return f"{message}\n"

    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate CLAUDE.md with tool instructions."""
        content = get_instructions_content(allowed_tools)
        return {"/workspace/CLAUDE.md": content}
```

#### FactoryDroidAgent

```python
@register_agent(AgentType.FACTORY_DROID)
class FactoryDroidAgent(AgentFactory):
    """Factory for Factory.ai Droid CLI agent."""

    def get_persistent_command(self) -> str:
        """Factory Droid в interactive режиме."""
        return "droid --interactive --unsafe-permissions"

    def get_tool_call_pattern(self) -> str:
        """Factory может использовать другой формат или тот же."""
        return r'__TOOL_CALL__:(?P<payload>\{.*\})'

    def parse_tool_call(self, match: re.Match) -> dict:
        """Same JSON parsing as Claude."""
        import json
        payload = match.group('payload')
        data = json.loads(payload)
        return {
            "tool": data["tool"],
            "args": data["args"],
            "raw": match.group(0)
        }

    def format_message_for_stdin(self, message: str) -> str:
        """Factory expects plain text."""
        return f"{message}\n"

    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate AGENTS.md for Factory Droid."""
        content = get_instructions_content(allowed_tools)
        return {"/workspace/AGENTS.md": content}

    def get_startup_timeout(self) -> int:
        """Factory может стартовать дольше."""
        return 45
```

#### CodexAgent (будущая реализация)

```python
@register_agent(AgentType.CODEX)
class CodexAgent(AgentFactory):
    """Factory for OpenAI Codex CLI agent."""

    def get_persistent_command(self) -> str:
        return "codex --stdin --format json"

    def get_tool_call_pattern(self) -> str:
        """Codex может использовать другой паттерн."""
        return r'TOOL:(?P<payload>\{.*\})'  # Different pattern!

    def parse_tool_call(self, match: re.Match) -> dict:
        import json
        payload = match.group('payload')
        data = json.loads(payload)
        return {
            "tool": data["action"],  # Different key name
            "args": data["parameters"],  # Different key name
            "raw": match.group(0)
        }

    def format_message_for_stdin(self, message: str) -> str:
        """Codex может требовать JSON envelope."""
        import json
        envelope = {"type": "user_message", "content": message}
        return json.dumps(envelope) + "\n"

    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Codex использует свой формат инструкций."""
        content = get_instructions_content(allowed_tools)
        return {"/workspace/.codex/instructions.txt": content}
```

### 3. ProcessManager (Generic)

**Отвечает за lifecycle persistent процессов.**

```python
class ProcessManager:
    """Manages persistent agent processes in containers.

    Generic implementation that works with any AgentFactory.
    """

    def __init__(self, container_service: ContainerService):
        self.container_service = container_service
        self._processes: dict[str, ProcessInfo] = {}
        # ProcessInfo = {
        #     "proc": asyncio.subprocess.Process,
        #     "factory": AgentFactory,
        #     "stdin": StreamWriter,
        #     "stdout": StreamReader,
        #     "stderr": StreamReader,
        #     "started_at": datetime,
        # }

    async def start_process(
        self,
        agent_id: str,
        factory: AgentFactory
    ) -> None:
        """Start persistent agent process using factory config.

        Args:
            agent_id: Container ID
            factory: Agent-specific factory instance

        Raises:
            RuntimeError: If process fails to start
        """
        # 1. Get command from factory (polymorphism!)
        command = factory.get_persistent_command()

        # 2. Start process in container
        docker_cmd = [
            "docker", "exec", "-i", agent_id,
            "/bin/bash", "-l", "-c", command
        ]

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 3. Wait for readiness (factory-specific check)
        timeout = factory.get_startup_timeout()
        readiness_check = factory.get_readiness_check()

        if readiness_check:
            await self._wait_for_ready(agent_id, readiness_check, timeout)
        else:
            # Default: wait for first stdout line
            await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=timeout
            )

        # 4. Store process info
        self._processes[agent_id] = {
            "proc": proc,
            "factory": factory,
            "stdin": proc.stdin,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "started_at": datetime.now(UTC),
        }

        logger.info(
            "persistent_process_started",
            agent_id=agent_id,
            agent_type=type(factory).__name__,
            command=command
        )

    async def write_to_stdin(self, agent_id: str, message: str) -> None:
        """Write message to agent's stdin.

        Uses factory to format message correctly.

        Args:
            agent_id: Container ID
            message: User message text

        Raises:
            ValueError: If process not found
        """
        if agent_id not in self._processes:
            raise ValueError(f"No process found for agent {agent_id}")

        process_info = self._processes[agent_id]
        factory = process_info["factory"]
        stdin = process_info["stdin"]

        # Format message using factory (polymorphism!)
        formatted = factory.format_message_for_stdin(message)

        # Write to stdin
        stdin.write(formatted.encode())
        await stdin.drain()

        logger.info(
            "message_written_to_stdin",
            agent_id=agent_id,
            message_length=len(message)
        )

    async def read_stdout_line(self, agent_id: str, timeout: float = 30.0) -> str | None:
        """Read one line from agent's stdout.

        Args:
            agent_id: Container ID
            timeout: Read timeout in seconds

        Returns:
            Line string or None if timeout/EOF
        """
        if agent_id not in self._processes:
            return None

        stdout = self._processes[agent_id]["stdout"]

        try:
            line_bytes = await asyncio.wait_for(
                stdout.readline(),
                timeout=timeout
            )
            return line_bytes.decode().rstrip('\n')
        except asyncio.TimeoutError:
            return None

    async def stop_process(self, agent_id: str, graceful: bool = True) -> None:
        """Stop persistent agent process.

        Args:
            agent_id: Container ID
            graceful: If True, send Ctrl+C, else SIGKILL
        """
        if agent_id not in self._processes:
            return

        proc = self._processes[agent_id]["proc"]

        if graceful:
            # Send Ctrl+C to stdin
            try:
                proc.stdin.write(b'\x03')
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        else:
            proc.kill()

        await proc.wait()
        del self._processes[agent_id]

        logger.info("persistent_process_stopped", agent_id=agent_id, graceful=graceful)
```

### 4. ToolCallListener (Agent-Aware)

**Отвечает за обнаружение и выполнение tool calls.**

```python
class ToolCallListener:
    """Listens to agent stdout and intercepts tool calls.

    Agent-aware: uses factory to parse tool calls correctly.
    """

    def __init__(
        self,
        process_manager: ProcessManager,
        tool_executor: ToolExecutor,
        log_collector: LogCollector
    ):
        self.process_manager = process_manager
        self.tool_executor = tool_executor
        self.log_collector = log_collector
        self._listening: dict[str, bool] = {}

    async def start_listening(self, agent_id: str, factory: AgentFactory) -> None:
        """Start listening to agent's stdout for tool calls.

        Args:
            agent_id: Container ID
            factory: Agent factory (provides pattern & parser)
        """
        self._listening[agent_id] = True

        # Get pattern from factory (polymorphism!)
        pattern = factory.get_tool_call_pattern()
        regex = re.compile(pattern)

        logger.info(
            "started_listening_for_tool_calls",
            agent_id=agent_id,
            pattern=pattern
        )

        while self._listening.get(agent_id):
            # Read line from stdout
            line = await self.process_manager.read_stdout_line(agent_id, timeout=1.0)

            if line is None:
                continue

            # Check for tool call
            match = regex.search(line)

            if match:
                # Parse using factory (polymorphism!)
                tool_call = factory.parse_tool_call(match)

                logger.info(
                    "tool_call_detected",
                    agent_id=agent_id,
                    tool=tool_call["tool"],
                    args=tool_call["args"]
                )

                # Execute tool
                await self._handle_tool_call(agent_id, tool_call)
            else:
                # Regular log line
                await self.log_collector.collect_stdout(agent_id, line)

    async def _handle_tool_call(self, agent_id: str, tool_call: dict) -> None:
        """Execute tool and return result to agent.

        Args:
            agent_id: Container ID
            tool_call: Parsed tool call dict
        """
        tool_name = tool_call["tool"]
        args = tool_call["args"]

        try:
            # Execute tool
            result = await self.tool_executor.execute(tool_name, args, agent_id)

            # Return result to agent via stdin
            if tool_name == "answer":
                # Special case: answer to user, no result to agent
                await self._send_answer_to_user(agent_id, args["message"])
            elif tool_name == "ask":
                # Escalation: wait for user input, return to agent
                user_response = await self._ask_user(agent_id, args["question"])
                await self.process_manager.write_to_stdin(
                    agent_id,
                    f"User answered: {user_response}"
                )
            else:
                # Other tools: return result
                result_text = f"Tool {tool_name} result: {result}"
                await self.process_manager.write_to_stdin(agent_id, result_text)

        except Exception as e:
            error_msg = f"Tool {tool_name} failed: {e}"
            await self.process_manager.write_to_stdin(agent_id, error_msg)
            logger.error(
                "tool_execution_failed",
                agent_id=agent_id,
                tool=tool_name,
                error=str(e)
            )

    async def stop_listening(self, agent_id: str) -> None:
        """Stop listening to agent's stdout."""
        self._listening[agent_id] = False
```

### 5. ToolExecutor

**Отвечает за выполнение инструментов.**

```python
class ToolExecutor:
    """Executes orchestrator tools (answer, ask, project, deploy, etc.)."""

    def __init__(self, redis_client, api_client):
        self.redis = redis_client
        self.api = api_client

    async def execute(self, tool_name: str, args: dict, agent_id: str) -> dict:
        """Execute a tool and return result.

        Args:
            tool_name: Tool name (answer, ask, project, deploy, etc.)
            args: Tool arguments
            agent_id: Agent container ID

        Returns:
            Tool execution result
        """
        if tool_name == "answer":
            return await self._execute_answer(args, agent_id)
        elif tool_name == "ask":
            return await self._execute_ask(args, agent_id)
        elif tool_name == "project":
            return await self._execute_project(args, agent_id)
        elif tool_name == "deploy":
            return await self._execute_deploy(args, agent_id)
        elif tool_name == "engineering":
            return await self._execute_engineering(args, agent_id)
        elif tool_name == "infra":
            return await self._execute_infra(args, agent_id)
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def _execute_answer(self, args: dict, agent_id: str) -> dict:
        """Send answer to user via Redis."""
        message = args["message"]

        # Publish to response stream
        await self.redis.xadd(
            "cli-agent:responses",
            {
                "agent_id": agent_id,
                "type": "answer",
                "message": message,
                "timestamp": datetime.now(UTC).isoformat()
            }
        )

        return {"success": True}

    async def _execute_ask(self, args: dict, agent_id: str) -> dict:
        """Escalate question to user."""
        question = args["question"]

        # Publish question to response stream
        await self.redis.xadd(
            "cli-agent:responses",
            {
                "agent_id": agent_id,
                "type": "question",
                "question": question,
                "timestamp": datetime.now(UTC).isoformat()
            }
        )

        # Block waiting for user answer
        # (Implementation depends on how we handle user responses)
        # For MVP, could use a Redis key: cli-agent:answer:{agent_id}

        return {"success": True, "waiting_for_answer": True}

    async def _execute_project(self, args: dict, agent_id: str) -> dict:
        """Call project API endpoint."""
        method = args.get("method")

        if method == "create":
            response = await self.api.post("/projects", json=args)
        elif method == "get":
            project_id = args["id"]
            response = await self.api.get(f"/projects/{project_id}")
        # ... etc

        return response.json()

    # Similar implementations for deploy, engineering, infra...
```

### 6. LogCollector

**Отвечает за сбор логов.**

```python
class LogCollector:
    """Collects and stores agent logs to Redis."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def collect_stdout(self, agent_id: str, line: str) -> None:
        """Collect stdout line."""
        await self._store_log(agent_id, "stdout", line)

    async def collect_stderr(self, agent_id: str, line: str) -> None:
        """Collect stderr line."""
        await self._store_log(agent_id, "stderr", line)

    async def _store_log(self, agent_id: str, stream: str, line: str) -> None:
        """Store log line to Redis stream."""
        await self.redis.xadd(
            f"agent:logs:{agent_id}",
            {
                "stream": stream,
                "line": line,
                "timestamp": datetime.now(UTC).isoformat()
            },
            maxlen=1000,  # Keep last 1000 lines
            approximate=True
        )
```

### 7. Orchestrator CLI Tools (Bash Wrappers)

**Устанавливаются в контейнере, доступны агенту.**

**`/usr/local/bin/orchestrator`:**

```bash
#!/bin/bash
# Orchestrator CLI tool wrapper
# Usage: orchestrator <command> [args...]

COMMAND="$1"
shift

case "$COMMAND" in
    answer)
        # orchestrator answer "message"
        MESSAGE="$1"
        echo "__TOOL_CALL__:{\"tool\":\"answer\",\"args\":{\"message\":\"$MESSAGE\"}}"
        ;;

    ask)
        # orchestrator ask "question"
        QUESTION="$1"
        echo "__TOOL_CALL__:{\"tool\":\"ask\",\"args\":{\"question\":\"$QUESTION\"}}"
        ;;

    project)
        # orchestrator project create --name myapp
        # Parse args and build JSON
        echo "__TOOL_CALL__:{\"tool\":\"project\",\"args\":{...}}"
        ;;

    deploy)
        echo "__TOOL_CALL__:{\"tool\":\"deploy\",\"args\":{...}}"
        ;;

    engineering)
        echo "__TOOL_CALL__:{\"tool\":\"engineering\",\"args\":{...}}"
        ;;

    infra)
        echo "__TOOL_CALL__:{\"tool\":\"infra\",\"args\":{...}}"
        ;;

    *)
        echo "Unknown command: $COMMAND" >&2
        exit 1
        ;;
esac
```

**Примечание**: Для Factory Droid и других агентов можно создать алиасы или wrapper с другим именем, но выходной формат остаётся единым.

---

## Пример работы (полиморфизм в действии)

### Пользователь отправляет сообщение:

```python
# Telegram Bot
await agent_manager.send_message(user_id=123, text="Create project myapp")
```

### Workers-Spawner обрабатывает:

```python
# CommandHandler
async def handle_send_message(request):
    agent_id = request["agent_id"]
    message = request["message"]

    # 1. Get agent metadata
    agent_meta = await container_service.get_metadata(agent_id)
    agent_type = agent_meta["agent_type"]  # e.g., CLAUDE_CODE or FACTORY_DROID

    # 2. Get factory (ПОЛИМОРФИЗМ!)
    factory = get_agent_factory(agent_type, container_service)

    # 3. Write to stdin (generic method)
    await process_manager.write_to_stdin(agent_id, message)

    # Factory handles formatting internally!
    # - ClaudeCodeAgent: "Create project myapp\n"
    # - FactoryDroidAgent: "Create project myapp\n"
    # - CodexAgent: "{\"type\":\"user_message\",\"content\":\"Create project myapp\"}\n"
```

### Agent обрабатывает и отвечает:

```
# Claude stdout:
I'll create the project for you.
__TOOL_CALL__:{"tool":"project","args":{"method":"create","name":"myapp"}}
Project created successfully!
__TOOL_CALL__:{"tool":"answer","args":{"message":"Done! Project 'myapp' created."}}
```

### ToolCallListener перехватывает:

```python
# Listening loop
while True:
    line = await process_manager.read_stdout_line(agent_id)

    # Get pattern from factory (ПОЛИМОРФИЗМ!)
    pattern = factory.get_tool_call_pattern()
    match = re.search(pattern, line)

    if match:
        # Parse using factory (ПОЛИМОРФИЗМ!)
        tool_call = factory.parse_tool_call(match)
        # {"tool": "project", "args": {"method": "create", "name": "myapp"}}

        # Execute tool
        await tool_executor.execute(tool_call["tool"], tool_call["args"], agent_id)
```

### Пользователь получает ответ:

```python
# Telegram Bot reads from Redis stream
message = await redis.xread({"cli-agent:responses": last_id})
# {"type": "answer", "message": "Done! Project 'myapp' created."}

await bot.send_message(user_id, message["message"])
```

---

## Фазы реализации MVP

### Phase 0: Design & Interfaces (2-3 дня)

**Цель**: Спроектировать интерфейсы и контракты.

**Задачи**:
1. ✅ Расширить `AgentFactory` abstract class новыми методами:
   - `get_persistent_command()`
   - `get_tool_call_pattern()`
   - `parse_tool_call()`
   - `format_message_for_stdin()`
   - `get_startup_timeout()`
   - `get_readiness_check()`

2. ✅ Определить tool call protocol:
   - Формат: `__TOOL_CALL__:{"tool":"name","args":{...}}`
   - Список tools: answer, ask, project, deploy, engineering, infra
   - JSON schema для каждого tool

3. ✅ Спроектировать ProcessManager API:
   - `start_process(agent_id, factory)`
   - `write_to_stdin(agent_id, message)`
   - `read_stdout_line(agent_id, timeout)`
   - `stop_process(agent_id, graceful)`

4. ✅ Спроектировать ToolCallListener API:
   - `start_listening(agent_id, factory)`
   - `stop_listening(agent_id)`

5. ✅ Спроектировать ToolExecutor API:
   - `execute(tool_name, args, agent_id)`

6. ✅ Написать спецификации в `docs/persistent-agents-mvp.md`

**Критерии готовности**:
- [ ] Все интерфейсы определены и задокументированы
- [ ] Tool call protocol полностью описан
- [ ] Написаны примеры для Claude Code и Factory Droid

---

### Phase 1: AgentFactory Extensions (2-3 дня)

**Цель**: Расширить существующие фабрики для persistent режима.

**Задачи**:

1. **Обновить базовый класс `AgentFactory`**:
   - Добавить новые abstract методы
   - Добавить default implementations где возможно
   - `services/workers-spawner/src/workers_spawner/factories/base.py`

2. **Реализовать ClaudeCodeAgent persistent методы**:
   ```python
   # services/workers-spawner/src/workers_spawner/factories/agents/claude_code.py

   def get_persistent_command(self) -> str:
       return "claude --dangerously-skip-permissions"

   def get_tool_call_pattern(self) -> str:
       return r'__TOOL_CALL__:(?P<payload>\{.*\})'

   def parse_tool_call(self, match: re.Match) -> dict:
       import json
       payload = match.group('payload')
       data = json.loads(payload)
       return {"tool": data["tool"], "args": data["args"], "raw": match.group(0)}

   def format_message_for_stdin(self, message: str) -> str:
       return f"{message}\n"
   ```

3. **Реализовать FactoryDroidAgent persistent методы**:
   ```python
   # services/workers-spawner/src/workers_spawner/factories/agents/factory_droid.py

   def get_persistent_command(self) -> str:
       return "droid --interactive --unsafe-permissions"

   # ... аналогично Claude, но может отличаться
   ```

4. **Создать CodexAgent заглушку** (для демонстрации полиморфизма):
   ```python
   # services/workers-spawner/src/workers_spawner/factories/agents/codex.py

   @register_agent(AgentType.CODEX)
   class CodexAgent(AgentFactory):
       """Stub implementation for future Codex support."""

       def get_persistent_command(self) -> str:
           return "codex --stdin --format json"

       # ... stub implementations
   ```

5. **Написать unit тесты**:
   - `services/workers-spawner/tests/unit/test_agent_factories_persistent.py`
   - Тесты для каждого метода каждой фабрики
   - Проверка полиморфизма

**Критерии готовности**:
- [ ] `AgentFactory` расширен новыми методами
- [ ] `ClaudeCodeAgent` реализует все persistent методы
- [ ] `FactoryDroidAgent` реализует все persistent методы
- [ ] `CodexAgent` stub создан
- [ ] Unit тесты покрывают >90% кода
- [ ] `make test-workers-spawner-unit` проходит

---

### Phase 2: ProcessManager Implementation (3-4 дня)

**Цель**: Реализовать управление persistent процессами.

**Задачи**:

1. **Создать ProcessManager**:
   ```python
   # services/workers-spawner/src/workers_spawner/process_manager.py

   class ProcessManager:
       """Manages persistent agent processes in containers."""

       def __init__(self, container_service: ContainerService):
           self.container_service = container_service
           self._processes: dict[str, ProcessInfo] = {}

       async def start_process(self, agent_id: str, factory: AgentFactory) -> None:
           """Start persistent agent process using factory config."""
           # Implementation as described above

       async def write_to_stdin(self, agent_id: str, message: str) -> None:
           """Write message to agent's stdin."""
           # Implementation as described above

       async def read_stdout_line(self, agent_id: str, timeout: float) -> str | None:
           """Read one line from stdout."""
           # Implementation as described above

       async def stop_process(self, agent_id: str, graceful: bool = True) -> None:
           """Stop process gracefully or forcefully."""
           # Implementation as described above
   ```

2. **Интеграция с ContainerService**:
   - Модифицировать `create_container()` для запуска persistent процесса
   - Добавить вызов `process_manager.start_process()` после создания контейнера
   - `services/workers-spawner/src/workers_spawner/container_service.py`

3. **Обработка ошибок**:
   - Retry logic для старта процесса
   - Graceful degradation если процесс умер
   - Auto-restart опция (опционально для MVP)

4. **Написать unit тесты**:
   - Mock ContainerService
   - Mock subprocess для тестирования
   - `services/workers-spawner/tests/unit/test_process_manager.py`

5. **Написать integration тесты**:
   - Реальный Docker контейнер
   - Запуск/остановка процесса
   - Запись в stdin, чтение из stdout
   - `services/workers-spawner/tests/integration/test_process_manager.py`

**Критерии готовности**:
- [ ] `ProcessManager` полностью реализован
- [ ] Интеграция с `ContainerService` работает
- [ ] Unit тесты покрывают >90% кода
- [ ] Integration тесты проходят с реальным контейнером
- [ ] Graceful shutdown работает корректно

---

### Phase 3: ToolCallListener & ToolExecutor (3-4 дня)

**Цель**: Реализовать обнаружение и выполнение tool calls.

**Задачи**:

1. **Создать ToolExecutor**:
   ```python
   # services/workers-spawner/src/workers_spawner/tool_executor.py

   class ToolExecutor:
       """Executes orchestrator tools."""

       def __init__(self, redis_client, api_client):
           self.redis = redis_client
           self.api = api_client

       async def execute(self, tool_name: str, args: dict, agent_id: str) -> dict:
           """Execute a tool and return result."""
           # Routing to specific tool handlers

       async def _execute_answer(self, args, agent_id) -> dict:
           """Publish answer to Redis response stream."""

       async def _execute_ask(self, args, agent_id) -> dict:
           """Escalate question to user."""

       async def _execute_project(self, args, agent_id) -> dict:
           """Call project API."""

       # ... etc for all tools
   ```

2. **Создать ToolCallListener**:
   ```python
   # services/workers-spawner/src/workers_spawner/tool_call_listener.py

   class ToolCallListener:
       """Listens to stdout and intercepts tool calls."""

       def __init__(self, process_manager, tool_executor, log_collector):
           self.process_manager = process_manager
           self.tool_executor = tool_executor
           self.log_collector = log_collector
           self._listening: dict[str, bool] = {}

       async def start_listening(self, agent_id: str, factory: AgentFactory) -> None:
           """Start listening loop for agent."""
           # Implementation as described above

       async def stop_listening(self, agent_id: str) -> None:
           """Stop listening."""
           self._listening[agent_id] = False
   ```

3. **Создать LogCollector**:
   ```python
   # services/workers-spawner/src/workers_spawner/log_collector.py

   class LogCollector:
       """Collects agent logs to Redis."""

       def __init__(self, redis_client):
           self.redis = redis_client

       async def collect_stdout(self, agent_id: str, line: str) -> None:
           """Store stdout line to Redis stream."""

       async def collect_stderr(self, agent_id: str, line: str) -> None:
           """Store stderr line to Redis stream."""
   ```

4. **Написать orchestrator CLI wrapper**:
   ```bash
   # services/universal-worker/orchestrator-cli/orchestrator

   #!/bin/bash
   # Wrapper for orchestrator tools
   # Outputs __TOOL_CALL__:{...} to stdout
   ```

5. **Обновить Dockerfile universal-worker**:
   - Копировать orchestrator script в `/usr/local/bin/`
   - Сделать executable
   - `services/universal-worker/Dockerfile`

6. **Написать unit тесты**:
   - Mock ProcessManager для ToolCallListener
   - Mock Redis для ToolExecutor
   - Тесты для каждого tool

7. **Написать integration тесты**:
   - Запуск контейнера с Claude
   - Отправка сообщения "use orchestrator answer 'test'"
   - Проверка что tool call перехвачен и выполнен

**Критерии готовности**:
- [ ] `ToolExecutor` реализован для всех tools
- [ ] `ToolCallListener` корректно парсит tool calls для разных агентов
- [ ] `LogCollector` сохраняет логи в Redis
- [ ] Orchestrator CLI wrapper работает
- [ ] Unit тесты покрывают >90%
- [ ] Integration тест end-to-end проходит

---

### Phase 4: Integration & Redis Handlers (2-3 дня)

**Цель**: Интегрировать всё в Redis command handlers.

**Задачи**:

1. **Обновить CommandHandler**:
   ```python
   # services/workers-spawner/src/workers_spawner/redis_handlers.py

   async def handle_create(request: dict) -> dict:
       """Create agent container and start persistent process."""
       # 1. Create container (existing logic)
       agent_id = await container_service.create_container(config, context)

       # 2. Get factory
       factory = get_agent_factory(config.agent, container_service)

       # 3. Start persistent process
       await process_manager.start_process(agent_id, factory)

       # 4. Start tool call listener
       asyncio.create_task(
           tool_call_listener.start_listening(agent_id, factory)
       )

       return {"agent_id": agent_id, "status": "running"}

   async def handle_send_message(request: dict) -> dict:
       """Send message to persistent process."""
       agent_id = request["agent_id"]
       message = request["message"]

       # Write to stdin (ProcessManager handles formatting via factory)
       await process_manager.write_to_stdin(agent_id, message)

       return {"success": True}

   async def handle_delete(request: dict) -> dict:
       """Stop persistent process and delete container."""
       agent_id = request["agent_id"]

       # 1. Stop listener
       await tool_call_listener.stop_listening(agent_id)

       # 2. Stop process
       await process_manager.stop_process(agent_id, graceful=True)

       # 3. Delete container (existing logic)
       await container_service.delete(agent_id)

       return {"success": True}
   ```

2. **Обновить session_manager**:
   - Удалить логику с session_id (больше не нужна)
   - Оставить только metadata контейнера
   - `services/workers-spawner/src/workers_spawner/session_manager.py`

3. **Создать dependency injection setup**:
   ```python
   # services/workers-spawner/src/workers_spawner/dependencies.py

   def setup_dependencies(redis_url: str, api_base_url: str):
       """Setup all dependencies with proper injection."""
       redis = redis.from_url(redis_url)
       api_client = httpx.AsyncClient(base_url=api_base_url)

       container_service = ContainerService()
       process_manager = ProcessManager(container_service)
       log_collector = LogCollector(redis)
       tool_executor = ToolExecutor(redis, api_client)
       tool_call_listener = ToolCallListener(
           process_manager,
           tool_executor,
           log_collector
       )

       return {
           "container_service": container_service,
           "process_manager": process_manager,
           "tool_call_listener": tool_call_listener,
           "tool_executor": tool_executor,
           "log_collector": log_collector,
       }
   ```

4. **Обновить main.py**:
   - Использовать новый dependency injection
   - `services/workers-spawner/src/main.py`

5. **Написать integration тесты**:
   - End-to-end: create → send_message → получить answer
   - Test с Claude Code
   - Test с Factory Droid
   - `services/workers-spawner/tests/integration/test_e2e_persistent.py`

**Критерии готовности**:
- [ ] Redis handlers обновлены
- [ ] Dependency injection настроен
- [ ] Integration тесты end-to-end проходят
- [ ] Claude Code и Factory Droid работают одинаково (полиморфизм!)

---

### Phase 5: Logging & Observability (1-2 дня)

**Цель**: Добавить proper logging и мониторинг.

**Задачи**:

1. **API endpoint для получения логов**:
   ```python
   # services/api/src/routers/agents.py

   @router.get("/agents/{agent_id}/logs")
   async def get_agent_logs(
       agent_id: str,
       limit: int = 100,
       offset: int = 0
   ):
       """Get agent logs from Redis."""
       logs = await redis.xrange(
           f"agent:logs:{agent_id}",
           count=limit,
           start=offset
       )
       return {"logs": logs}
   ```

2. **Добавить structured logging во все компоненты**:
   - ProcessManager: process_started, message_written, process_stopped
   - ToolCallListener: tool_call_detected, tool_executed
   - ToolExecutor: tool_answer, tool_ask, tool_project, etc.

3. **Metrics (опционально для MVP)**:
   - Количество активных процессов
   - Latency tool execution
   - Tool call distribution

4. **Health check endpoint**:
   ```python
   # services/workers-spawner/src/main.py

   @app.get("/health")
   async def health():
       return {
           "status": "healthy",
           "active_processes": len(process_manager._processes),
           "listening_agents": len(tool_call_listener._listening)
       }
   ```

**Критерии готовности**:
- [ ] API endpoint `/agents/{agent_id}/logs` работает
- [ ] Structured logging во всех компонентах
- [ ] Health check endpoint отвечает
- [ ] Логи содержат agent_id, tool, timestamp

---

### Phase 6: Testing & Stabilization (2-3 дня)

**Цель**: Полное тестирование и баг фиксы.

**Задачи**:

1. **End-to-end тестирование**:
   - Создать агента через Telegram Bot
   - Отправить сообщение "Create project test-app"
   - Проверить что tool calls выполнились
   - Проверить что ответ пришёл в Telegram
   - Повторить с Factory Droid

2. **Stress тестирование**:
   - 10 одновременных агентов
   - Отправка 100 сообщений подряд
   - Проверка memory leaks

3. **Error scenario тестирование**:
   - Процесс упал (SIGSEGV)
   - Контейнер перезапустился
   - Redis недоступен
   - Tool execution timeout

4. **Документация**:
   - Обновить README с инструкциями
   - Обновить ARCHITECTURE.md
   - Примеры использования
   - Troubleshooting guide

5. **Баг фиксы**:
   - Исправление найденных багов
   - Performance оптимизации

**Критерии готовности**:
- [ ] E2E тест проходит для Claude и Factory
- [ ] Stress тест выдержан (10+ агентов)
- [ ] Error scenarios обработаны gracefully
- [ ] Документация обновлена
- [ ] Нет critical багов

---

### Phase 7: Rollout (1 день)

**Цель**: Деплой на production.

**Задачи**:

1. **Создать Docker образы**:
   ```bash
   make build
   docker tag workers-spawner:latest workers-spawner:persistent-mvp
   ```

2. **Deploy на staging**:
   - Тестирование на staging окружении
   - Smoke тесты

3. **Deploy на production**:
   - Blue-green deployment
   - Мониторинг метрик

4. **Announcement**:
   - Объявление о новой фиче
   - Migration guide для существующих пользователей

**Критерии готовности**:
- [ ] Docker образы собраны
- [ ] Staging deployment успешен
- [ ] Production deployment успешен
- [ ] Мониторинг показывает healthy статус

---

## Timeline Estimate

| Phase | Duration | Dependencies |
|-------|----------|--------------|
| 0. Design & Interfaces | 2-3 дня | None |
| 1. AgentFactory Extensions | 2-3 дня | Phase 0 |
| 2. ProcessManager | 3-4 дня | Phase 1 |
| 3. ToolCallListener & ToolExecutor | 3-4 дня | Phase 2 |
| 4. Integration & Redis Handlers | 2-3 дня | Phase 3 |
| 5. Logging & Observability | 1-2 дня | Phase 4 |
| 6. Testing & Stabilization | 2-3 дня | Phase 5 |
| 7. Rollout | 1 день | Phase 6 |

**Total**: 16-23 дня (3-4 недели)

С учётом параллельной работы и overlap между фазами: **~3 недели реального времени**.

---

## Success Criteria

### Functional Requirements

- [ ] **Agent abstraction**: Любой CLI агент работает через единый интерфейс
- [ ] **Полиморфизм**: Можно добавить новый агент, реализовав AgentFactory
- [ ] **Persistent процессы**: Один процесс живёт 2+ часа
- [ ] **Tool-based communication**: Ответы приходят через tool calls
- [ ] **Logging**: Все логи собираются и доступны через API
- [ ] **No session_id**: Сессия = persistent процесс
- [ ] **Graceful shutdown**: Процессы останавливаются корректно

### Non-Functional Requirements

- [ ] **Performance**: Response time <30 секунд
- [ ] **Reliability**: Uptime >99% для persistent процессов
- [ ] **Scalability**: Поддержка 10+ одновременных агентов на одном workers-spawner
- [ ] **Maintainability**: Code coverage >90% для unit тестов
- [ ] **Extensibility**: Добавление нового агента <1 дня работы

### Business Requirements

- [ ] **Демонстрация полиморфизма**: Claude и Factory работают одинаково с точки зрения пользователя
- [ ] **Документация**: Новый разработчик может добавить агента за 1 день
- [ ] **Пользовательский опыт**: Пользователь не замечает разницы между агентами

---

## Risks & Mitigation

### Risk 1: Process stability

**Проблема**: Persistent процесс может упасть (SIGSEGV, OOM).

**Mitigation**:
- Мониторинг process health
- Auto-restart при падении
- Graceful degradation (fallback на ephemeral)

### Risk 2: Agent-specific quirks

**Проблема**: Разные CLI агенты могут иметь несовместимые форматы.

**Mitigation**:
- Чёткое определение tool call protocol
- Фабрики инкапсулируют agent-specific логику
- Тестирование с 2+ агентами в MVP

### Risk 3: Memory leaks

**Проблема**: Persistent процессы могут накапливать память.

**Mitigation**:
- TTL для контейнеров (2 часа)
- Monitoring memory usage
- Graceful restart при threshold

### Risk 4: Tool execution timeout

**Проблема**: Tool может выполняться долго (deploy, engineering).

**Mitigation**:
- Async tool execution
- Status updates через Redis
- Timeout handling с retry

---

## Future Enhancements (Post-MVP)

### 1. Context Window Management

**Проблема**: Persistent процесс может упереться в context limit.

**Решение**:
- Context compaction
- Summary generation
- Rolling window

### 2. Multi-Agent Communication

**Проблема**: Agent-to-agent routing (Analyst → Engineer).

**Решение**:
- Tool `orchestrator route_to_agent`
- Agent registry в Redis
- Message queue между агентами

### 3. Streaming Logs UI

**Проблема**: Логи нужно смотреть в реальном времени.

**Решение**:
- WebSocket endpoint
- Redis pubsub для логов
- React component для UI

### 4. Advanced Observability

**Проблема**: Нужна детальная аналитика.

**Решение**:
- Prometheus metrics
- Grafana dashboards
- Distributed tracing (Jaeger)

### 5. Scale-Adaptive Intelligence

**Проблема**: Для простых задач нужен 1 агент, для сложных — команда.

**Решение**:
- Complexity estimator
- Dynamic team composition
- Hierarchical routing (из BMAD roadmap)

---

## Appendix

### A. Tool Call Protocol Specification

**Format**: `__TOOL_CALL__:{"tool": "<name>", "args": {<args>}}`

**Tools**:

1. **answer** - Отправить ответ пользователю
   ```json
   {"tool": "answer", "args": {"message": "Hello user!"}}
   ```

2. **ask** - Задать вопрос пользователю (эскалация)
   ```json
   {"tool": "ask", "args": {"question": "Which database to use?"}}
   ```

3. **project** - CRUD операции с проектами
   ```json
   {"tool": "project", "args": {"method": "create", "name": "myapp", "template": "fastapi"}}
   {"tool": "project", "args": {"method": "get", "id": "proj_123"}}
   {"tool": "project", "args": {"method": "update", "id": "proj_123", "status": "deployed"}}
   ```

4. **deploy** - Запуск деплоя
   ```json
   {"tool": "deploy", "args": {"project_id": "proj_123", "server_id": "srv_456"}}
   ```

5. **engineering** - Запуск Engineering субграфа
   ```json
   {"tool": "engineering", "args": {"task": "implement feature X", "project_id": "proj_123"}}
   ```

6. **infra** - Запуск Infrastructure субграфа
   ```json
   {"tool": "infra", "args": {"task": "setup server", "server_id": "srv_456"}}
   ```

### B. AgentFactory Interface Full Spec

См. секцию "Детальный дизайн компонентов" выше.

### C. ProcessManager State Machine

```
[INIT] → start_process() → [STARTING] → readiness check → [RUNNING]
                                 ↓
                              [FAILED] → retry or error

[RUNNING] → write_to_stdin() → [RUNNING]
         → read_stdout_line() → [RUNNING]
         → stop_process(graceful=True) → [STOPPING] → [STOPPED]
         → stop_process(graceful=False) → [KILLED]
         → process died → [CRASHED]
```

### D. Comparison: Ephemeral vs Persistent

| Aspect | Ephemeral (Current) | Persistent (MVP) |
|--------|---------------------|------------------|
| Process lifecycle | 1 process per message | 1 process per container lifetime (2h) |
| History | Lost between calls | Preserved in process memory |
| Session management | Redis session_id | No session_id needed |
| Communication | Output parsing | Tool calls + logs |
| Latency | ~5-10s per call | <1s (stdin write) |
| Complexity | Medium | Low (simpler!) |
| Agent support | Claude-specific | Any CLI agent (polymorphic) |

---

## Conclusion

Этот MVP план фокусируется на создании универсальной системы persistent CLI-агентов с правильной абстракцией через фабрики и полиморфизм.

**Ключевые отличия от предыдущего плана**:
1. ✅ Фокус на абстракции и полиморфизме
2. ✅ AgentFactory как центральный интерфейс
3. ✅ ProcessManager generic (работает с любым агентом)
4. ✅ ToolCallListener agent-aware (использует фабрику)
5. ✅ Поддержка минимум 2 агентов в MVP (Claude + Factory)
6. ✅ Пользователь использует единый API (send_message), детали скрыты

**Результат**: Система, в которую можно легко добавить новый CLI агент (Codex, Gemini, custom), просто реализовав AgentFactory интерфейс.
