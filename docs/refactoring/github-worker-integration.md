# GitHub Worker Integration Plan

**Цель**: Расширить workers-spawner для поддержки GitHub operations (clone, commit, push) с возможностью переключения между Claude Code и Factory.ai одной переменной.

**Дата создания**: 2026-01-08  
**Статус**: Draft

---

## Текущее Состояние

### Что работает
- ✅ Telegram → workers-spawner → Claude Code (headless mode)
- ✅ `GitHubAppClient` получает installation tokens
- ✅ `DeveloperNode.spawn_worker()` вызывает `request_spawn()`

### Что НЕ работает
- ❌ Git credentials не настроены в контейнере
- ❌ `request_spawn()` использует старый протокол (`send_command` вместо `send_message`)
- ❌ Нет capability "github" в workers-spawner

---

## Архитектурные Принципы

### 1. Универсальность workers-spawner

```
WorkerConfig {
  agent: "claude-code" | "factory-droid"   ← Одна переменная!
  capabilities: ["git", "github", "python", "node"]
  allowed_tools: ["project", "deploy", "orchestrator_cli"]
  env_vars: { GITHUB_TOKEN, REPO_NAME }
}
```

### 2. Handling Long-Running Tasks (CRITICAL)

**Проблема**: Base Claude Code headless имеет ограничения:
- Shell command timeout: 2 минуты
- Agent timeout: 10 минут

**Задачи типа clone + code + test + commit + push занимают 10-20+ минут.**

**Решения**:

| Агент | Подход | Стоимость | Приоритет |
|-------|--------|-----------|-----------|
| Claude Code (base) | ❌ Не подходит для длительных задач | - | NOT VIABLE |
| Claude Code + ralph-wiggum | ✅ Автономная работа 10+ минут через stop-hook | Pro подписка ($20/мес) | **PRIMARY** |
| Factory.ai Droid | ✅ Нативно поддерживает долгие задачи | API calls (дорого) | **SECONDARY** |

**Стратегия**:
- **Development**: Claude Code + ralph-wiggum (используем Pro подписку)
- **Production**: Factory.ai Droid (если нужна стабильность) или Claude API
- **Requirement**: Оба агента ОБЯЗАНЫ работать автономно минимум 10 минут

### 3. Capability как Composable Unit

Каждая capability добавляет:
- APT packages
- Install commands
- Environment variables
- Setup files

```python
# Пример: GitHubCapability
class GitHubCapability(CapabilityFactory):
    def get_apt_packages(self) -> list[str]:
        return []  # git уже есть в GitCapability
    
    def get_install_commands(self) -> list[str]:
        return []
    
    def get_env_vars(self) -> dict[str, str]:
        return {}  # Token передаётся через WorkerConfig.env_vars
    
    def get_setup_commands(self, env_vars: dict) -> list[str]:
        """Setup git credentials if GITHUB_TOKEN present."""
        if "GITHUB_TOKEN" not in env_vars:
            return []
        return [
            'git config --global credential.helper store',
            f'echo "https://x-access-token:$GITHUB_TOKEN@github.com" > ~/.git-credentials',
            'git config --global user.email "bot@vladmesh.dev"',
            'git config --global user.name "Codegen Bot"',
        ]
```

### 4. Agent Polymorphism

```
AgentFactory (abstract)
├── ClaudeCodeAgent (PRIMARY для dev)
│   ├── send_message_headless() → claude -p "..." --output-format json --resume
│   ├── Требует: ralph-wiggum plugin для длительных задач
│   └── Стоимость: Pro подписка $20/мес
└── FactoryDroidAgent (SECONDARY для prod)
    ├── send_message_headless() → droid exec -o json "..."
    ├── Преимущество: нативно поддерживает долгие задачи
    └── Стоимость: API calls (дорого для разработки)
```

Смена агента = смена `agent: "claude-code"` → `agent: "factory-droid"` в config.

**Для GitHub worker дефолт `agent: "claude-code"`** с ralph-wiggum для разработки.

---

## Итеративный План Выполнения

### Фаза 1: GitHub Capability (1 час)

#### Шаг 1.1: Создать GitHubCapability

**Файл**: `services/workers-spawner/src/workers_spawner/factories/capabilities/github.py`

```python
"""GitHub capability for git operations with authentication."""

from workers_spawner.factories.base import CapabilityFactory
from workers_spawner.factories.registry import register_capability
from workers_spawner.models import Capability


@register_capability(Capability.GITHUB)
class GitHubCapability(CapabilityFactory):
    """Adds GitHub authentication for git push/pull operations.
    
    Requires GITHUB_TOKEN in env_vars for authentication.
    Works with GitHub App installation tokens or PATs.
    """

    def get_apt_packages(self) -> list[str]:
        # git is already provided by GitCapability
        return []

    def get_install_commands(self) -> list[str]:
        return []

    def get_env_vars(self) -> dict[str, str]:
        return {}


def get_github_setup_commands(env_vars: dict[str, str]) -> list[str]:
    """Get git credential setup commands.
    
    Called by ContainerService after container creation.
    """
    token = env_vars.get("GITHUB_TOKEN")
    if not token:
        return []
    
    return [
        'git config --global credential.helper store',
        # Token is in env, so use variable reference
        'echo "https://x-access-token:${GITHUB_TOKEN}@github.com" > ~/.git-credentials',
        'git config --global user.email "bot@vladmesh.dev"',
        'git config --global user.name "Codegen Bot"',
    ]
```

**Критерий**: GitHubCapability создан и зарегистрирован.

---

#### Шаг 1.2: Добавить Capability.GITHUB в models

**Файл**: `services/workers-spawner/src/workers_spawner/models.py`

```python
class Capability(str, Enum):
    GIT = "git"
    CURL = "curl"
    PYTHON = "python"
    NODE = "node"
    GITHUB = "github"  # ← ДОБАВИТЬ
```

**Критерий**: Enum расширен.

---

#### Шаг 1.3: Интегрировать setup_commands в ContainerService

**Файл**: `services/workers-spawner/src/workers_spawner/container_service.py`

После создания контейнера и записи instruction files, выполнить setup commands:

```python
async def create_container(self, config: WorkerConfig, context: dict) -> str:
    # ... existing container creation ...
    
    # NEW: Execute capability setup commands
    await self._run_capability_setup(agent_id, config)
    
    return agent_id

async def _run_capability_setup(self, agent_id: str, config: WorkerConfig) -> None:
    """Run post-creation setup for capabilities."""
    from workers_spawner.factories.capabilities.github import get_github_setup_commands
    
    if Capability.GITHUB in config.capabilities:
        env_vars = config.env_vars or {}
        commands = get_github_setup_commands(env_vars)
        for cmd in commands:
            await self.send_command(agent_id, cmd, timeout=10)
```

**Критерий**: Git credentials настраиваются при создании контейнера с GITHUB capability.

---

### Фаза 2: Ralph-Wiggum для Claude Code (2-3 часа) - PRIMARY

**Цель**: Обеспечить автономную работу Claude Code минимум 10 минут через ralph-wiggum plugin.

#### Шаг 2.1: Добавить ralph-wiggum в universal-worker

**Файл**: `services/universal-worker/Dockerfile`

```dockerfile
# Install ralph-wiggum plugin for long-running Claude tasks
RUN npm install -g @anthropic-ai/ralph-wiggum
```

**Критерий**: Плагин установлен в контейнере.

---

#### Шаг 2.2: Обновить ClaudeCodeAgent для stop-hook pattern

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/claude_code.py`

**Критическое требование**: Реализовать resumption loop для автономной работы 10+ минут.

```python
async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Send message using headless mode with ralph-wiggum for long tasks.

    Ralph-wiggum enables multi-step autonomous work through stop-hook pattern:
    1. Agent works until reaches stopping point
    2. Returns JSON with is_stopped=true
    3. We automatically resume with --resume
    4. Repeat until task complete or timeout
    """
    session_id = session_context.get("session_id") if session_context else None

    start_time = time.time()
    accumulated_response = []

    while True:
        elapsed = time.time() - start_time
        remaining_timeout = timeout - int(elapsed)

        if remaining_timeout <= 0:
            raise RuntimeError(f"Task timeout after {timeout} seconds")

        cmd_parts = [
            "claude",
            "-p", shlex.quote(message) if not session_id else '""',  # Empty prompt on resume
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]

        if session_id:
            cmd_parts.extend(["--resume", session_id])

        full_command = " ".join(cmd_parts)

        logger.info(
            "claude_headless_step",
            agent_id=agent_id,
            has_session=bool(session_id),
            elapsed_seconds=int(elapsed),
            remaining_timeout=remaining_timeout,
        )

        # Execute with remaining timeout
        result = await self.container_service.send_command(
            agent_id, full_command, timeout=remaining_timeout
        )

        # Parse JSON response
        try:
            data = json.loads(result.output)

            # Check for error
            if data.get("is_error", False):
                error_msg = data.get("result", "Unknown Claude error")
                raise RuntimeError(f"Claude error: {error_msg}")

            # Accumulate response
            response_text = data.get("result", "")
            if response_text:
                accumulated_response.append(response_text)

            # Update session for next iteration
            session_id = data.get("session_id")

            # Check if stopped (ralph-wiggum stop-hook)
            is_stopped = data.get("is_stopped", False)

            if is_stopped:
                logger.info(
                    "claude_stopped_resuming",
                    agent_id=agent_id,
                    session_id=session_id,
                    steps_completed=len(accumulated_response),
                )
                # Continue loop to resume
                continue
            else:
                # Task complete
                logger.info(
                    "claude_task_complete",
                    agent_id=agent_id,
                    total_steps=len(accumulated_response),
                    total_time=int(elapsed),
                )

                return {
                    "response": "\n\n".join(accumulated_response),
                    "session_context": {"session_id": session_id},
                    "metadata": {
                        "usage": data.get("usage", {}),
                        "model": data.get("model"),
                        "steps": len(accumulated_response),
                        "elapsed_seconds": int(elapsed),
                    },
                }

        except json.JSONDecodeError as e:
            logger.error(
                "failed_to_parse_json",
                agent_id=agent_id,
                exit_code=result.exit_code,
                output_preview=result.output[:500],
                error=str(e),
            )
            error_detail = result.output[:200] if result.output else result.error or "Unknown error"
            raise RuntimeError(f"Claude CLI failed: {error_detail}") from e
```

**Критерий**: Claude Code может работать 10+ минут автономно через resumption loop.

---

#### Шаг 2.3: Добавить timeout parameter в базовый класс

**Файл**: `services/workers-spawner/src/workers_spawner/factories/base.py`

```python
@abstractmethod
async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
    timeout: int = 120,  # ← NEW: configurable timeout
) -> dict[str, Any]:
    """Send message to agent in headless mode.

    Args:
        timeout: Max execution time in seconds (default 2 minutes)
    """
```

**Критерий**: Timeout настраивается для длительных задач.

---

#### Шаг 2.4: Обновить redis_handlers.py для timeout

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

```python
async def _handle_send_message(self, message: dict[str, Any]) -> dict[str, Any]:
    """Handle send_message command using headless mode."""
    agent_id = message.get("agent_id")
    user_message = message.get("message")
    timeout = message.get("timeout", 120)  # ← NEW: accept timeout from caller

    # ... existing code ...

    # Send message via factory's headless method
    result = await factory.send_message_headless(
        agent_id=agent_id,
        message=user_message,
        session_context=session_context,
        timeout=timeout,  # ← Pass timeout to factory
    )
```

**Критерий**: Timeout передаётся из LangGraph через Redis в factory.

---

### Фаза 3: Обновить LangGraph worker_spawner Client (1.5 часа)

#### Шаг 3.1: Мигрировать на send_message headless mode

**Файл**: `services/langgraph/src/clients/worker_spawner.py`

**Было**:
```python
# Использует send_command для low-level execution
cmd_payload = {
    "command": "send_command",
    "shell_command": 'claude --dangerously-skip-permissions -p "$(cat ...)"',
}
```

**Стало**:
```python
async def request_spawn(
    repo: str,
    github_token: str,
    task_content: str,
    task_title: str = "AI generated changes",
    agent_type: str = "claude-code",  # Default: Claude Code + ralph-wiggum для dev
    timeout_seconds: int = 900,  # 15 минут для clone + code + test + push
) -> SpawnResult:
    # 1. Create container with GITHUB capability
    config = {
        "agent": agent_type,  # "claude-code" (default) or "factory-droid" (prod)
        "capabilities": ["git", "github", "node", "python"],
        "allowed_tools": ["project"],
        "env_vars": {
            "GITHUB_TOKEN": github_token,
            "REPO_NAME": repo,
        },
        "mount_session_volume": False,
    }

    # 2. Create
    create_resp = await _send_command("create", {"config": config, ...})
    agent_id = create_resp["agent_id"]

    try:
        # 3. Clone repo (git credentials already setup by capability)
        # Use shallow clone for speed
        clone_cmd = f"cd /workspace && git clone --depth 1 https://github.com/{repo}.git ."
        await _send_command("send_command", {"agent_id": agent_id, "shell_command": clone_cmd})

        # 4. Send task via headless message with timeout
        # NOTE: Claude Code + ralph-wiggum supports 10+ minute autonomous tasks
        # Factory.ai Droid нативно поддерживает длительные задачи
        task_message = f"""
{task_title}

{task_content}

After completing the task:
1. Commit all changes with descriptive message
2. Push to the repository
"""

        msg_resp = await _send_command("send_message", {
            "agent_id": agent_id,
            "message": task_message,
            "timeout": timeout_seconds,  # Pass timeout to redis handler
        })

        # 5. Parse result
        return SpawnResult(
            success=msg_resp.get("success", False),
            output=msg_resp.get("response", ""),
            metadata=msg_resp.get("metadata", {}),
        )

    finally:
        # Cleanup container after task completion or failure
        await _send_command("delete", {"agent_id": agent_id})
```

**Критерий**: `request_spawn()` использует `send_message` headless mode с timeout и cleanup.

---

### Фаза 4: Реализовать FactoryDroidAgent - SECONDARY для продакшена (1 час)

**Цель**: Подготовить альтернативу для продакшена, если Pro подписка не подходит.

#### Шаг 4.1: Полноценная реализация send_message_headless

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/factory_droid.py`

```python
async def send_message_headless(
    self,
    agent_id: str,
    message: str,
    session_context: dict | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """Send message to Factory Droid agent.

    Factory.ai Droid нативно поддерживает длительные автономные задачи.
    Может работать часами без дополнительных плагинов.
    """
    import shlex

    # Factory.ai droid exec command
    # Note: Factory uses ANTHROPIC_API_KEY from env
    cmd = f"/home/worker/.local/bin/droid exec -o json {shlex.quote(message)}"

    # Factory handles long tasks internally, но мы передаём timeout для safety
    result = await self.container_service.send_command(
        agent_id, cmd, timeout=timeout
    )

    if result.exit_code != 0:
        raise RuntimeError(f"Droid exec failed: {result.error}")

    try:
        data = json.loads(result.output)
        return {
            "response": data.get("result", result.output),
            "session_context": session_context,
            "metadata": data.get("metadata", {}),
        }
    except json.JSONDecodeError:
        return {
            "response": result.output,
            "session_context": session_context,
            "metadata": {},
        }
```

**Критерий**: FactoryDroidAgent работает с headless mode и длительными задачами.

---

#### Шаг 4.2: Добавить ANTHROPIC_API_KEY для Factory.ai

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/factory_droid.py`

```python
def get_required_env_vars(self) -> list[str]:
    """Factory.ai Droid requires API key."""
    return ["ANTHROPIC_API_KEY"]
```

> **Примечание**: Для Factory.ai нужен API key, для Claude Code — OAuth session.

**Критерий**: Factory.ai использует свои credentials.

---

### Фаза 5: Тестирование (2 часа)

**Критическое требование**: Оба агента (Claude Code + ralph-wiggum и Factory.ai) должны работать минимум 10 минут автономно.

#### Шаг 5.1: Unit Test для GitHubCapability

**Файл**: `services/workers-spawner/tests/unit/test_github_capability.py`

```python
def test_github_setup_commands_with_token():
    from workers_spawner.factories.capabilities.github import get_github_setup_commands
    
    env = {"GITHUB_TOKEN": "ghs_xxx", "REPO_NAME": "org/repo"}
    commands = get_github_setup_commands(env)
    
    assert len(commands) == 4
    assert "credential.helper store" in commands[0]
    assert "git-credentials" in commands[1]

def test_github_setup_commands_without_token():
    commands = get_github_setup_commands({})
    assert commands == []
```

---

#### Шаг 5.2: Integration Test для Claude Code + ralph-wiggum (PRIMARY)

**Тест сценарий**:
1. Создать контейнер с `agent: "claude-code"` и `capabilities: ["git", "github"]`
2. Проверить что ralph-wiggum установлен
3. Отправить `send_message` с задачей на 10+ минут (clone, code, test, commit, push)
4. Убедиться что resumption loop работает (несколько is_stopped=true итераций)
5. Проверить что итоговый коммит запушен

**Критерий**: Claude Code работает автономно минимум 10 минут.

---

#### Шаг 5.3: Integration Test для Factory.ai Droid (SECONDARY)

**Тест сценарий**:
1. Создать контейнер с `agent: "factory-droid"` и `capabilities: ["git", "github"]`
2. Отправить `send_message` с задачей на 10+ минут
3. Проверить что итоговый коммит запушен

**Критерий**: Factory.ai Droid работает автономно минимум 10 минут.

---

#### Шаг 5.4: E2E через DeveloperNode

```python
# В langgraph можно вызвать:
result = await request_spawn(
    repo="vladmesh-projects/test-repo",
    github_token=token,
    task_content="Clone repo, create README.md, run tests, commit and push",
    agent_type="claude-code",  # Default, использует ralph-wiggum
    timeout_seconds=900,  # 15 минут для автономной работы
)
assert result.success
assert result.metadata.get("steps", 0) > 1  # Несколько resumption итераций
```

**Критерий**: E2E тест проходит с Claude Code + ralph-wiggum.

---

### Фаза 6: Документация и Cleanup (30 мин)

#### Шаг 6.1: Обновить CLAUDE.md

Добавить секцию про:
- GitHub capability
- Ralph-wiggum для длительных задач с Claude Code
- Timeout handling
- Рекомендации: Claude Code (dev) vs Factory.ai (prod)
- Requirement: оба агента работают минимум 10 минут автономно

#### Шаг 6.2: Commit

```bash
git commit -m "feat(workers-spawner): add GitHub capability with long-running task support

- Add GitHubCapability with credential setup
- Implement ralph-wiggum resumption loop for Claude Code (10+ min autonomous work)
- Migrate request_spawn to send_message headless mode
- Add configurable timeout for long-running tasks
- Add FactoryDroidAgent as secondary option for production
- Default to claude-code for GitHub worker (uses Pro subscription)
- Add cleanup strategy and shallow clone optimization
- Both agents verified to work autonomously for 10+ minutes

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>
"
```

---

## Диаграмма: Целевая Архитектура

```
DeveloperNode
    │
    ▼
request_spawn(repo, github_token, agent_type="claude-code", timeout=900)
    │
    ▼
workers-spawner (Redis)
    │
    ├─ create container
    │   ├─ capabilities: [git, github, python, node]
    │   ├─ env_vars: {GITHUB_TOKEN, REPO_NAME}
    │   └─ agent: "claude-code" (default) или "factory-droid" (prod)
    │
    ├─ run capability setup (git credentials)
    │
    ├─ git clone --depth 1 repo
    │
    ├─ send_message (headless mode, timeout=900s)
    │   └─ AgentFactory.send_message_headless(timeout=900)
    │       ├─ ClaudeCodeAgent (PRIMARY для dev):
    │       │   └─ RESUMPTION LOOP (ralph-wiggum):
    │       │       ├─ claude -p ... --output-format json --resume
    │       │       ├─ Check is_stopped=true → resume
    │       │       ├─ Accumulate response
    │       │       └─ Repeat until complete (10+ минут автономно)
    │       │
    │       └─ FactoryDroidAgent (SECONDARY для prod):
    │           └─ droid exec -o json ... (нативная поддержка долгих задач)
    │
    └─ delete container (cleanup)
```

---

## Оценка Трудозатрат

| Фаза | Задача | Время | Накопительно | Приоритет |
|------|--------|-------|--------------|-----------|
| 1 | GitHub Capability | 1 час | 1 час | **MUST** |
| 2 | Ralph-Wiggum для Claude Code | 2-3 часа | 3-4 часа | **MUST** |
| 3 | LangGraph Client (timeout, cleanup) | 1.5 часа | 4.5-5.5 часов | **MUST** |
| 4 | FactoryDroidAgent (secondary) | 1 час | 5.5-6.5 часов | **MUST** |
| 5 | Тестирование (оба агента 10+ мин) | 2 часа | 7.5-8.5 часов | **MUST** |
| 6 | Документация | 30 мин | 8-9 часов | **MUST** |
| **ИТОГО** | | **~8-9 часов** | - | - |

**Критические требования**:
- ✅ Claude Code + ralph-wiggum работает автономно минимум 10 минут (PRIMARY)
- ✅ Factory.ai Droid работает автономно минимум 10 минут (SECONDARY)
- ✅ Default agent: `"claude-code"` (используем Pro подписку для разработки)
- ✅ Resumption loop реализован для Claude Code
- ✅ Timeout configurable через Redis message

---

## Критерии Успеха

- ✅ `capabilities: ["github"]` настраивает git credentials
- ✅ **Claude Code + ralph-wiggum работает автономно минимум 10 минут** (PRIMARY)
- ✅ **Factory.ai Droid работает автономно минимум 10 минут** (SECONDARY)
- ✅ Resumption loop реализован и протестирован (is_stopped → resume)
- ✅ `agent: "claude-code"` используется по умолчанию
- ✅ `agent: "factory-droid"` переключение работает для продакшена
- ✅ Clone + Task (10+ мин) + Commit + Push работает через headless mode
- ✅ Timeout configurable (по умолчанию 900 секунд)
- ✅ Cleanup strategy реализован (finally block)
- ✅ Shallow clone оптимизация (--depth 1)
- ✅ Никаких agent-specific деталей в LangGraph клиенте
- ✅ E2E тест проходит для обоих агентов

---

## Риски

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| Ralph-wiggum не работает как ожидается | Средняя | Тщательное тестирование resumption loop, fallback на Factory.ai |
| is_stopped logic неправильно реализован | Средняя | Unit тесты, проверка JSON response format |
| Claude Code Pro подписка недоступна | Низкая | Переключение на Factory.ai для разработки |
| Factory.ai droid CLI не поддерживает `-o json` | Низкая | Fallback на plain text parsing (уже реализовано) |
| Token expires mid-task (>40 мин) | Низкая | Installation tokens живут 1 час, задачи обычно <15 мин |
| Large repos slow to clone | Средняя | Shallow clone: `git clone --depth 1` (реализовано) |
| Timeout не передаётся корректно | Низкая | Integration тесты с различными timeout значениями |

---

**Автор**: Claude Sonnet 4.5
**Статус**: Updated - Ralph-wiggum is PRIMARY, Factory.ai is SECONDARY
**Дата обновления**: 2026-01-08
**Критическое требование**: Оба агента ОБЯЗАНЫ работать автономно минимум 10 минут

