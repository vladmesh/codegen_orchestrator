# Workers Spawner Service

Унифицированный сервис для управления CLI-агентами (Claude Code, Factory.ai Droid и др.) в изолированных Docker контейнерах.

## Мотивация

**Проблема:**
- Текущие решения (`agent-spawner`, `worker-spawner`) дублируют код управления контейнерами.
- Жесткая привязка инструментов к Python-коду (Tool classes) мешает создавать новых агентов "на лету".
- Админу/PO сложно создать экспериментального воркера без правки кода сервиса.

**Решение:**
- **Легковесный Spawner:** Управляет жизненным циклом контейнеров (Create, Pause, Resume, Delete).
- **Умный CLI:** Вся логика инструментов внутри `orchestrator-cli` в контейнере.
- **Декларативность:** Конфиг отвечает на вопрос "ЧТО нужно", а не "КАК это сделать".
- **Интерактивность:** Контейнеры persistent, общение через stdin.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Workers Spawner                               │
│                                                                      │
│  ┌────────────────────┐     ┌─────────────────────────────────────┐  │
│  │ (cli-agent.*)      │                                             │
│  └─────────┬──────────┘                                             │
│            │                                                         │
│            ▼                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                    Container Manager                             │ │
│  │  • create(config) → agent_id                                     │ │
│  │  • send_command(agent_id, stdin_input)                           │ │
│  │  • send_file(agent_id, path, content)                            │ │
│  │  • status(agent_id) → state                                      │ │
│  │  • logs(agent_id) → output                                       │ │
│  │  • delete(agent_id)                                              │ │
│  └─────────┬───────────────────────────────────────────────────────┘ │
│            │                                                         │
│            ▼                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                 Agent/Capability Factories                       │ │
│  │  • AgentFactory: claude-code, factory-droid, codex, gemini-cli   │ │
│  │  • CapabilityFactory: git, curl, node, python, etc.              │ │
│  │  (Знают КАК установить и настроить каждый компонент)             │ │
│  └─────────┬───────────────────────────────────────────────────────┘ │
│            │ Docker API                                              │
└────────────┼─────────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Agent Container                                 │
└─────────────────────────────────────────────────────────────────────┘
```

## Spawner API

Взаимодействие через Redis streams.

### Commands (Request → Response)

| Command | Description |
|---------|-------------|
| `cli-agent.create` | Создать новый контейнер по конфигу |
| `cli-agent.send_command` | Отправить stdin в контейнер |
| `cli-agent.send_file` | Записать файл в контейнер |
| `cli-agent.status` | Получить статус контейнера |
| `cli-agent.logs` | Получить логи контейнера |
| `cli-agent.delete` | Удалить контейнер |

### Request Payloads

**cli-agent.create:**
```json
{
  "request_id": "req_123",
  "config": {
    "name": "Developer (Claude Code)",
    "agent": "claude-code",
    "capabilities": ["git", "curl"],
    "allowed_tools": ["project", "engineering"],
    "env_vars": {
        "OPENAI_API_KEY": "sk-..."
    }
  },
  "context": {
    "user_id": "user_1",
    "project_id": "proj_abc"
  }
}
```

**cli-agent.send_command:**
```json
{
  "request_id": "req_124",
  "agent_id": "agent_xyz",
  "command": "Read TASK.md and implement it"
}
```

**cli-agent.send_file:**
```json
{
  "request_id": "req_125",
  "agent_id": "agent_xyz",
  "path": "/workspace/TASK.md",
  "content": "# Task\n\nImplement feature X..."
}
```

### Events (PubSub)

| Channel | Event | Description |
|---------|-------|-------------|
| `agents:{agent_id}:response` | Agent output | `orchestrator respond` вызовы |
| `agents:{agent_id}:command_exit` | Command finished | Exit code команды |
| `agents:{agent_id}:status` | State change | idle → running → idle |

## Container Lifecycle

```
create ──► [running: init] ──► [idle: paused] ◄──┐
                                    │            │
                          send_command           │
                                    │            │
                                    ▼            │
                              [running: busy] ───┘
                                    │      command_exit
                                    │
                              TTL expired / delete
                                    │
                                    ▼
                                 [deleted]
```

**Паузинг контейнеров:**
- Используем `docker pause` / `docker unpause` для экономии ресурсов.
- Контейнер в состоянии `paused` не потребляет CPU, но сохраняет state.
- **Внимание:** При удалении контейнера (`delete` или TTL) все локальные данные теряются. Агент должен вывести важные данные (через `respond` или артефакты) перед завершением. Контейнеры эфемерильны.

## Worker Configuration

Конфиг отвечает на вопрос **"ЧТО нужно"**, не "как это сделать".

### Пример конфига

```json
{
  "id": "developer_claude",
  "name": "Developer (Claude Code)",
  
  "agent": "claude-code",
  
  "capabilities": ["git", "curl"],
  
  "allowed_tools": ["project", "engineering", "respond"],
  
  "has_internet": true,
  
  "ttl_hours": 2,
  "timeout_minutes": 10
}
```

### Поля

| Field | Required | Description |
|-------|----------|-------------|
| `id` | ✓ | Уникальный идентификатор конфига |
| `name` | ✓ | Человекочитаемое имя |
| `agent` | ✓ | Тип агента (enum) |
| `capabilities` | | Дополнительные возможности (enum list) |
| `allowed_tools` | ✓ | Разрешенные команды orchestrator-cli |
| `has_internet` | | Доступ к интернету (default: true для MVP) |
| `ttl_hours` | | Время жизни контейнера (default: 2) |
| `timeout_minutes` | | Таймаут на одну команду (default: 10) |

### Agent Types (enum)

Сервис **знает как** настроить каждый тип агента:

| Agent | Description |
|-------|-------------|
| `claude-code` | Anthropic Claude Code CLI |
| `factory-droid` | Factory.ai Droid |
| `codex` | OpenAI Codex CLI |
| `gemini-cli` | Google Gemini CLI |

### Capabilities (enum)

Сервис **знает как** установить каждую capability:

| Capability | Description |
|------------|-------------|
| `git` | Version control |
| `curl` | HTTP requests |
| `node` | Node.js runtime |
| `python` | Python 3 runtime |
| `docker` | Docker CLI (for special agents) |

### Allowed Tools

Список разрешенных команд `orchestrator-cli`:

| Tool | Commands |
|------|----------|
| `project` | `orchestrator project list/get/create/update` |
| `deploy` | `orchestrator deploy trigger/status/logs` |
| `engineering` | `orchestrator engineering trigger/status` |
| `infra` | `orchestrator infra allocate/list` |
| `respond` | `orchestrator respond "message"` |
| `admin` | `orchestrator admin ...` (dangerous) |

## Internal: Factories

Spawner использует фабрики для преобразования декларативного конфига в конкретные действия.

```python
class AgentFactory(ABC):
    @abstractmethod
    def get_install_commands(self) -> list[str]: ...
    
    @abstractmethod
    def get_agent_command(self) -> str: ...

class ClaudeCodeAgent(AgentFactory):
    def get_install_commands(self):
        return [
            "npm install -g @anthropic-ai/claude-code"
        ]
    
    def get_agent_command(self):
        return "claude --dangerously-skip-permissions"

class CapabilityFactory(ABC):
    @abstractmethod
    def get_packages(self) -> list[str]: ...

class GitCapability(CapabilityFactory):
    def get_packages(self):
        return ["git"]
```

## Status Response

```json
{
  "agent_id": "agent_xyz",
  "state": "idle",
  "created_at": "2026-01-01T16:00:00Z",
  "last_activity": "2026-01-01T16:05:00Z",
  "ttl_remaining_sec": 3600,
  "metrics": {
      "memory_mb": 128
  }
}
```

**States:**
- `initializing` — контейнер запускается
- `idle` — ready, ожидает команду (paused)
- `running` — выполняет команду
- `error` — initialization failed или crash
- `deleted` — контейнер удалён

**Client-Side Presets:**
Пресеты (например `developer_claude`, `po_claude`) хранятся на стороне клиента (или вызывающего сервиса), а не в Spawner. Spawner получает только итоговый JSON.

```json
/* Пример того, что отправляет клиент */
{
    "name": "Product Owner",
    "agent": "claude-code",
    "capabilities": ["git"],
    // ...
}
```

**Переключение Developer агента — одна строка:** `"agent": "claude-code"` → `"agent": "factory-droid"`.
