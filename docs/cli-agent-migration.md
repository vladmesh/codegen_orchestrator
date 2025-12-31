# CLI Agent Migration Plan

Замена LangGraph Product Owner на CLI-агента (Claude Code / Factory.ai Droid / Codex).

## Мотивация

| Аспект | LangGraph PO | CLI Agent |
|--------|--------------|-----------|
| **Token efficiency** | Все tools в каждом запросе | Skills загружаются по необходимости |
| **Flexibility** | Жёсткая структура графа | Свободная навигация |
| **Context management** | Ручной checkpointing | Суммаризация из коробки |
| **User isolation** | Shared state, manual filtering | Container per user |
| **Tool configuration** | Python code + registry | Markdown skills |

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                        Telegram Bot                              │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Session Manager                             │
│  - Container lifecycle (create/pause/resume/destroy)            │
│  - Message routing (Telegram ↔ Container)                       │
│  - State persistence (Redis)                                     │
│  - User → Container mapping                                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
│  User Container 1 │ │  User Container 2 │ │  User Container N │
│                   │ │                   │ │                   │
│  ┌─────────────┐  │ │  ┌─────────────┐  │ │  ┌─────────────┐  │
│  │ Claude Code │  │ │  │ Claude Code │  │ │  │ Claude Code │  │
│  │ (or Droid)  │  │ │  │ (or Droid)  │  │ │  │ (or Droid)  │  │
│  └──────┬──────┘  │ │  └──────┬──────┘  │ │  └──────┬──────┘  │
│         │         │ │         │         │ │         │         │
│  ~/.claude/       │ │  ~/.claude/       │ │  ~/.claude/       │
│  ├── skills/      │ │  ├── skills/      │ │  ├── skills/      │
│  ├── settings.json│ │  ├── settings.json│ │  ├── settings.json│
│  └── CLAUDE.md    │ │  └── CLAUDE.md    │ │  └── CLAUDE.md    │
│         │         │ │         │         │ │         │         │
│  orchestrator-cli │ │  orchestrator-cli │ │  orchestrator-cli │
└─────────┬─────────┘ └─────────┬─────────┘ └─────────┬─────────┘
          │                     │                     │
          └─────────────────────┴─────────────────────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              PostgreSQL                  Redis
              (projects, secrets,    (events, queues,
               allocations)           session state)
```

## Фазы миграции

---

## Phase 1: Infrastructure Setup

**Цель:** Базовая инфраструктура для запуска CLI-агента в контейнере.

### 1.1 Base Container Image

Создать `services/agent-worker/Dockerfile`:

```dockerfile
FROM node:20-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates curl && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Workspace
WORKDIR /workspace
ENV HOME=/home/node

# Disable telemetry
ENV DISABLE_TELEMETRY=1
ENV DISABLE_ERROR_REPORTING=1
ENV DISABLE_AUTOUPDATER=1

# Use existing node user (uid=1000)
USER node

ENTRYPOINT ["claude"]
```

### 1.2 Session Authentication

Для разработки (подписка):
```bash
# Прокинуть сессию через volume mount (read-write для session persistence)
docker run --rm \
  -v ~/.claude:/home/node/.claude \
  agent-worker --dangerously-skip-permissions -p "test" --output-format json
```

Для прода (API key):
```bash
docker run --rm \
  -e ANTHROPIC_API_KEY="$API_KEY" \
  agent-worker --dangerously-skip-permissions -p "test" --output-format json
```

> **Note:** `--dangerously-skip-permissions` обязателен для non-interactive режима.

### 1.3 Container Orchestration Service

Создать `services/agent-spawner/` с ephemeral container подходом:

```python
# services/agent-spawner/src/container_manager.py

class ContainerManager:
    """Manages ephemeral Docker containers for CLI agents.
    
    Uses docker run --rm for each execution.
    Session continuity maintained via Claude's --resume flag.
    """

    async def execute(self, user_id: str, prompt: str, session_id: str | None = None):
        """Execute prompt in ephemeral container."""
        cmd = [
            "docker", "run", "--rm",
            f"--network={self.settings.container_network}",
            "-v", f"{self.settings.host_claude_dir}:/home/node/.claude",
            self.settings.agent_image,
            "--dangerously-skip-permissions",
            "-p", prompt,
            "--output-format", "json",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])
        
        # Execute and parse JSON response
        result = await asyncio.create_subprocess_exec(*cmd, ...)
        return ExecutionResult(output=parsed["result"], session_id=parsed["session_id"])
```

> **Архитектурное решение:** Используем ephemeral containers (`docker run --rm`) вместо persistent containers, потому что Claude CLI без аргументов сразу завершается. Session persistence обеспечивается через `--resume` флаг.

**Deliverables:**
- [x] `services/agent-worker/Dockerfile`
- [x] `services/agent-spawner/` service
- [x] Docker compose integration
- [x] Basic health checks

---

## Phase 2: Orchestrator CLI

**Цель:** CLI инструмент внутри контейнера для взаимодействия с orchestrator API.

### 2.1 CLI Structure

```
orchestrator-cli/
├── orchestrator
├── commands/
│   ├── project.py      # list, get, create, update
│   ├── deploy.py       # trigger, status, logs
│   ├── infra.py        # servers, allocations
│   ├── engineering.py  # trigger, status, pr
│   ├── diagnose.py     # logs, health, incidents
│   └── graph.py        # trigger-node, clear-state
└── client.py           # API client
```

### 2.2 Commands Mapping

| Current Tool | CLI Command |
|--------------|-------------|
| `respond_to_user(msg)` | `orchestrator respond "msg"` |
| `search_knowledge(query)` | `orchestrator search "query"` |
| `finish_task(summary)` | `orchestrator finish "summary"` |
| `list_projects()` | `orchestrator project list` |
| `get_project_status(id)` | `orchestrator project status <id>` |
| `trigger_deploy(id)` | `orchestrator deploy trigger <id>` |
| `get_deploy_logs(id)` | `orchestrator deploy logs <id>` |
| `delegate_to_analyst(req)` | `orchestrator engineering analyze "req"` |
| `trigger_engineering(id)` | `orchestrator engineering trigger <id>` |
| `get_service_logs(svc)` | `orchestrator diagnose logs <service>` |

### 2.3 Output Format

Все команды возвращают структурированный вывод:

```bash
$ orchestrator project list
┌────────────────────────────────────────────────────────────┐
│ Projects for user vlad                                     │
├──────┬─────────────────┬────────────┬─────────────────────┤
│ ID   │ Name            │ Status     │ Last Updated        │
├──────┼─────────────────┼────────────┼─────────────────────┤
│ 42   │ todo-app        │ deployed   │ 2024-01-15 14:30    │
│ 43   │ analytics-svc   │ developing │ 2024-01-15 16:45    │
└──────┴─────────────────┴────────────┴─────────────────────┘

$ orchestrator deploy trigger 42 --json
{"task_id": "deploy-abc123", "status": "queued", "project_id": 42}
```

### 2.4 Authentication

CLI получает user context через environment:

```python
# Внутри контейнера
USER_ID = os.environ["ORCHESTRATOR_USER_ID"]
API_TOKEN = os.environ["ORCHESTRATOR_API_TOKEN"]
API_URL = os.environ["ORCHESTRATOR_API_URL"]
```

**Deliverables:**
- [ ] `orchestrator-cli` Python package
- [ ] All command implementations
- [ ] API client with auth
- [ ] Unit tests

---

## Phase 3: Skills System

**Цель:** Заменить capability registry на Claude Code skills.

### 3.1 Skill Structure

```
~/.claude/skills/
├── deploy.md           # Deployment operations
├── infrastructure.md   # Server management
├── engineering.md      # Code generation workflow
├── project.md          # Project management
├── diagnose.md         # Troubleshooting
└── admin.md            # Administrative operations
```

### 3.2 Skill Template

```markdown
---
name: deploy
description: Deploy projects to production servers
---

# Deploy Skill

You help users deploy their projects to production.

## Available Commands

### Check Deployment Readiness
```bash
orchestrator deploy check <project_id>
```
Returns: missing secrets, server allocation status, build status.

### Trigger Deployment
```bash
orchestrator deploy trigger <project_id>
```
Starts deployment pipeline. Returns task_id for tracking.

### Get Deployment Status
```bash
orchestrator deploy status <task_id>
```
Returns: current step, logs, errors.

### View Deployment Logs
```bash
orchestrator deploy logs <project_id> [--lines=100]
```

## Workflow

1. First check readiness with `deploy check`
2. If missing user secrets, ask user to provide them
3. Trigger deployment with `deploy trigger`
4. Monitor with `deploy status` until complete
5. Report deployed URL to user

## Common Issues

- **Missing secrets**: Ask user for values, don't generate
- **Port conflict**: Use `orchestrator infra allocate-port`
- **Build failure**: Check logs, may need engineering fix
```

### 3.3 Skills vs Current Capabilities

| Capability | Skill File | Commands |
|------------|------------|----------|
| `deploy` | `deploy.md` | `deploy check/trigger/status/logs` |
| `infrastructure` | `infrastructure.md` | `infra list/allocate/release` |
| `project_management` | `project.md` | `project list/get/create/update` |
| `engineering` | `engineering.md` | `engineering analyze/trigger/status/pr` |
| `diagnose` | `diagnose.md` | `diagnose logs/health/incidents` |
| `admin` | `admin.md` | `admin nodes/trigger/clear` |

### 3.4 Dynamic Skill Loading

Skills загружаются по необходимости:
- User: "deploy my app" → `deploy.md` loaded
- User: "show me logs" → `diagnose.md` loaded
- User: "create new project" → `project.md` + `engineering.md` loaded

**Deliverables:**
- [ ] All skill markdown files
- [ ] Skills testing in container
- [ ] Documentation for skill authoring

---

## Phase 4: Container CLAUDE.md

**Цель:** Системный промпт для агента внутри контейнера.

### 4.1 CLAUDE.md Structure

```markdown
# Orchestrator Agent

You are an AI assistant helping users build and deploy software projects.

## Your Role

- Help users create, develop, and deploy projects
- Use orchestrator-cli commands to interact with the system
- Never expose internal implementation details to users
- Always confirm destructive actions before executing

## Architecture

- You run inside an isolated container
- Each user has their own container instance
- Use skills for specialized workflows
- Use orchestrator-cli for all system interactions

## Communication Style

- Be concise and direct
- Use markdown formatting for readability
- Show command outputs when relevant
- Ask clarifying questions when requirements are unclear

## Available Skills

Use `/skill-name` to activate specialized workflows:
- `/deploy` - Deploy projects to production
- `/engineering` - Start code generation
- `/diagnose` - Troubleshoot issues
- `/infrastructure` - Manage servers and resources

## Security Rules

1. Never output secrets or credentials
2. Never execute commands outside orchestrator-cli
3. Never access files outside /workspace
4. Always validate user permissions before actions

## Error Handling

- If a command fails, explain the error clearly
- Suggest next steps for resolution
- Escalate to human if blocked after 3 attempts
```

**Deliverables:**
- [ ] `CLAUDE.md` template
- [ ] Per-project customization logic
- [ ] Testing with real conversations

---

## Phase 5: Message Bridge

**Цель:** Связать Telegram Bot с agent containers.

### 5.1 Message Flow

```
Telegram Bot
    │
    ▼
Redis Stream: user:{user_id}:incoming
    │
    ▼
Agent Spawner (reads stream)
    │
    ├─ Container exists? → docker unpause
    ├─ Container paused? → docker unpause
    └─ No container? → docker create
    │
    ▼
claude -p "{message}" --resume {session_id} --output-format json
    │
    ▼
Parse JSON output
    │
    ▼
Redis Stream: user:{user_id}:outgoing
    │
    ▼
Telegram Bot (sends to user)
```

### 5.2 Session Management

```python
class SessionManager:
    async def handle_message(self, user_id: str, message: str):
        # Get or create session
        session = await self.get_session(user_id)

        # Resume conversation
        result = await self.spawner.execute(
            user_id=user_id,
            prompt=message,
            session_id=session.claude_session_id,
            output_format="json"
        )

        # Parse result
        parsed = json.loads(result.stdout)

        # Update session
        session.claude_session_id = parsed["session_id"]
        await self.save_session(session)

        # Send response
        await self.send_to_telegram(user_id, parsed["result"])
```

### 5.3 Container Lifecycle

```
┌─────────────────────────────────────────────────────────┐
│                    Container States                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────┐   message    ┌─────────┐                  │
│  │  PAUSED  │ ───────────▶ │ RUNNING │                  │
│  └──────────┘              └────┬────┘                  │
│       ▲                         │                       │
│       │                         │ idle 5min             │
│       │                         ▼                       │
│       │                    ┌─────────┐                  │
│       └────────────────────│ PAUSED  │                  │
│                            └────┬────┘                  │
│                                 │                       │
│                                 │ idle 24h              │
│                                 ▼                       │
│                           ┌───────────┐                 │
│                           │ DESTROYED │                 │
│                           └───────────┘                 │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Deliverables:**
- [ ] Message bridge service
- [ ] Session persistence
- [ ] Container lifecycle automation
- [ ] Telegram bot integration

---

## Phase 6: Graph Triggers

**Цель:** CLI команды которые запускают LangGraph subgraphs.

### 6.1 Preserved Subgraphs

Сохраняем как отдельные LangGraph процессы:
- Engineering Subgraph (Architect → Preparer → Developer → Tester)
- DevOps Subgraph (EnvAnalyzer → SecretResolver → Deployer)

### 6.2 Trigger Mechanism

```python
# orchestrator-cli/commands/engineering.py

@cli.command()
def trigger(project_id: int):
    """Trigger engineering subgraph for project."""
    # Publish to Redis
    redis.xadd("engineering:queue", {
        "project_id": project_id,
        "user_id": os.environ["ORCHESTRATOR_USER_ID"],
        "callback_stream": f"user:{user_id}:events"
    })

    task_id = generate_task_id()
    click.echo(f"Engineering started. Task ID: {task_id}")
    click.echo(f"Monitor with: orchestrator engineering status {task_id}")
```

### 6.3 Event Streaming

Agent получает updates через Redis:

```python
# Inside container, agent can poll for updates
@cli.command()
def status(task_id: str, follow: bool = False):
    """Get engineering task status."""
    if follow:
        # Stream events
        for event in redis.xread(f"task:{task_id}:events"):
            click.echo(format_event(event))
    else:
        # Get current status
        status = api.get_task_status(task_id)
        click.echo(format_status(status))
```

**Deliverables:**
- [ ] Queue-based subgraph triggers
- [ ] Event streaming to containers
- [ ] Status polling commands

---

## Phase 7: Agent Abstraction Layer

**Цель:** Возможность заменить Claude Code на другой CLI agent.

### 7.1 Agent Interface

```python
# services/agent-spawner/src/agents/base.py

class BaseAgent(ABC):
    @abstractmethod
    async def execute(
        self,
        prompt: str,
        session_id: str | None = None,
        output_format: str = "json"
    ) -> AgentResult:
        """Execute prompt and return result."""
        pass

    @abstractmethod
    async def get_session_id(self, result: AgentResult) -> str:
        """Extract session ID for continuation."""
        pass


class AgentResult:
    stdout: str
    stderr: str
    exit_code: int
    session_id: str | None
    structured_output: dict | None
```

### 7.2 Implementations

```python
# Claude Code
class ClaudeCodeAgent(BaseAgent):
    async def execute(self, prompt, session_id=None, output_format="json"):
        cmd = ["claude", "-p", prompt, f"--output-format={output_format}"]
        if session_id:
            cmd.extend(["--resume", session_id])
        return await self._run(cmd)

# Factory.ai Droid
class DroidAgent(BaseAgent):
    async def execute(self, prompt, session_id=None, output_format="json"):
        cmd = ["droid", "--task", prompt]
        if session_id:
            cmd.extend(["--session", session_id])
        return await self._run(cmd)

# OpenAI Codex (hypothetical)
class CodexAgent(BaseAgent):
    async def execute(self, prompt, session_id=None, output_format="json"):
        cmd = ["codex", "run", prompt]
        return await self._run(cmd)
```

### 7.3 Agent Selection

```python
# Configuration
AGENT_TYPE = os.getenv("AGENT_TYPE", "claude-code")

def get_agent() -> BaseAgent:
    agents = {
        "claude-code": ClaudeCodeAgent,
        "droid": DroidAgent,
        "codex": CodexAgent,
    }
    return agents[AGENT_TYPE]()
```

**Deliverables:**
- [ ] Agent abstraction interface
- [ ] Claude Code implementation
- [ ] Droid implementation (already exists, adapt)
- [ ] Configuration system

---

## Phase 8: Migration & Cleanup

**Цель:** Полный переход и удаление старого кода.

### 8.1 Parallel Running

1. Добавить feature flag `USE_CLI_AGENT`
2. Роутинг в Telegram Bot:
   ```python
   if settings.USE_CLI_AGENT:
       await agent_spawner.send_message(user_id, message)
   else:
       await langgraph_worker.invoke(user_id, message)
   ```
3. A/B тестирование на части пользователей

### 8.2 Deprecation

После стабилизации:
- [ ] Remove `services/langgraph/src/nodes/product_owner.py`
- [ ] Remove `services/langgraph/src/nodes/intent_parser.py`
- [ ] Remove `services/langgraph/src/capabilities/`
- [ ] Remove most tools (keep subgraph triggers)
- [ ] Update documentation

### 8.3 Keep

- Engineering Subgraph
- DevOps Subgraph
- API service
- Database schema
- Redis infrastructure

---

## Testing Strategy

### Unit Tests
- [ ] Orchestrator CLI commands
- [ ] Agent abstraction layer
- [ ] Session management

### Integration Tests
- [ ] Container lifecycle
- [ ] Message flow Telegram → Container → Telegram
- [ ] Subgraph triggers

### E2E Tests
- [ ] Full user journey: create project → develop → deploy
- [ ] Multi-turn conversations
- [ ] Error recovery

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Claude Code prompt interference | Custom CLAUDE.md overrides, testing |
| Container resource exhaustion | Aggressive pause/destroy lifecycle |
| Session loss on container restart | Redis session persistence + resume |
| Latency on cold start | Container pool, predictive warming |
| Skill confusion | Clear descriptions, testing |
| Structured output parsing | JSON schema validation, fallbacks |

---

## Success Metrics

- **Token efficiency**: 50%+ reduction in tokens per conversation
- **Response latency**: <5s for cached containers, <15s cold start
- **Reliability**: 99% successful message processing
- **User satisfaction**: Qualitative feedback

---

## Timeline Estimates

> Note: Estimates are rough, adjust based on team velocity.

| Phase | Effort |
|-------|--------|
| Phase 1: Infrastructure | 1 week |
| Phase 2: CLI | 1 week |
| Phase 3: Skills | 3-4 days |
| Phase 4: CLAUDE.md | 2 days |
| Phase 5: Message Bridge | 1 week |
| Phase 6: Graph Triggers | 3-4 days |
| Phase 7: Abstraction | 2-3 days |
| Phase 8: Migration | 1 week |
| **Total** | **~5-6 weeks** |
