# Workers Spawner Service

Унифицированный сервис для управления CLI-агентами (Claude Code, Factory.ai Droid и др.) в изолированных Docker контейнерах.

## Мотивация

**Проблема:**
- Текущие решения (`agent-spawner`, `worker-spawner`) дублируют код управления контейнерами.
- Жесткая привязка инструментов к Python-коду (Tool classes) мешает создавать новых агентов "на лету".
- Админу/PO сложно создать экспериментального воркера без правки кода сервиса.

**Решение:**
- **Легковесный Spawner:** Занимается только жизненным циклом контейнеров (Spawn, TTL, Kill).
- **Умный CLI:** Вся логика инструментов внутри `orchestrator-cli` в контейнере.
- **Декларативность:** Конфиг воркера — это JSON. Права — это список разрешенных команд.
- **Динамические навыки:** Spawner генерирует документацию (`CLAUDE.md`, `/skills`) под конкретного агента, фильтруя лишнее.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                      Workers Spawner                             │
│                                                                  │
│  ┌──────────────────────┐   ┌──────────────────────────────┐    │
│  │ Redis Stream Listener│   │      Config Registry         │    │
│  │ (workers:spawn)      │◄──┤ (JSON presets / DB)          │    │
│  └──────────┬───────────┘   └──────────────────────────────┘    │
│             │                                                    │
│             ▼                                                    │
│  ┌──────────────────────┐                                        │
│  │   Worker Factory     │                                        │
│  │ 1. Load attributes   │                                        │
│  │ 2. Generate skills   │                                        │
│  │ 3. Docker Run        │                                        │
│  └──────────┬───────────┘                                        │
│             │                                                    │
└─────────────┼────────────────────────────────────────────────────┘
              │ Manage Lifecycle (TTL)
              ▼
┌─────────────────────────────────────────────────┐
│ Worker Container                                │
│                                                 │
│  ┌───────────────┐  Read   ┌─────────────────┐  │
│  │  CLI Agent    │ ──────► │ ~/.claude/      │  │
│  │ (Claude/Droid)│         │   skills/       │  │
│  └──────┬────────┘         │   CLAUDE.md     │  │
│         │ Exec             └─────────────────┘  │
│         ▼                                       │
│  ┌───────────────┐ Enforce ┌─────────────────┐  │
│  │OrchestratorCLI│ ──────► │ Env Vars:       │  │
│  │(tools logic)  │         │ ALLOWED_TOOLS=..│  │
│  └──────┬────────┘         └─────────────────┘  │
│         │ API / Redis                           │
│         ▼                                       │
│    Orchestrator Backend                         │
└─────────────────────────────────────────────────┘
```

## Worker Configuration Model

Конфигурация — это JSON-схема. Нет жестких Python Enums, блокирующих рантайм-создание.

### Schema

```json
{
  "id": "po_claude_v1",
  "name": "Product Owner (Claude)",
  
  // Base environment
  "image": "universal-worker-base:latest",
  "system_packages": [
    "git", 
    "curl", 
    "npm", 
    "python3-pip"
  ],
  "bootstrap_commands": [
    "npm install -g @anthropic-ai/claude-code",
    "pip install orchestrator-cli" 
  ],

  // Agent Runtime
  "agent": {
    "type": "claude-code", // affects CLAUDE.md generation style
    "command": ["claude", "--dangerously-skip-permissions", "-p", "{prompt}"]
  },

  // Security & Tools
  "allowed_tools": [
    "project",      // Allow 'orchestrator project *'
    "deploy",       // Allow 'orchestrator deploy *'
    "respond"       // Allow 'orchestrator respond'
  ],
  
  // Access Control
  "network": "codegen_orchestrator_internal",
  "has_internet": true,
  
  // Resources
  "limits": {
    "cpu": 2.0,
    "memory": "4g",
    "timeout_sec": 600,
    "ttl_sec": 7200
  }
}
```

### Принцип работы Allowed Tools

Поле `allowed_tools` управляет двумя вещами:

1.  **Генерация документации (Context Management):**
    Spawner копирует в контейнер только соответствующие Markdown-файлы.
    *   `"deploy"` -> копирует `skills/deploy.md`
    *   `"admin"` -> копирует `skills/admin.md`
    *   `CLAUDE.md` генерируется со списком только доступных команд.

2.  **Enforcement (Security):**
    Spawner устанавливает ENV переменную `ORCHESTRATOR_ALLOWED_TOOLS=project,deploy`.
    `orchestrator-cli` при запуске проверяет этот список. Попытка выполнить `orchestrator admin nuke` упадет с ошибкой "Permission denied", даже если агент "угадал" команду.

## Orchestrator CLI (`services/orchestrator-cli`)

Единая точка входа для всех инструментов. Переезжает из `agent-worker` в отдельный пакет.

### Структура команд
*   `orchestrator project [list|get|create]`
*   `orchestrator deploy [trigger|status|logs]`
*   `orchestrator engineering [trigger|status]`
*   `orchestrator infra [allocate|list]`
*   `orchestrator respond "[message]"`

### Обязанности
1.  Аутентификация (через `ORCHESTRATOR_USER_ID`, `ORCHESTRATOR_API_KEY`).
2.  Авторизация команд (через `ORCHESTRATOR_ALLOWED_TOOLS`).
3.  Форматирование вывода (JSON/Human readable).

## Container Base Image

Используем один универсальный образ, который донастраивается при старте (или пре-билдится для скорости).

```dockerfile
# services/universal-worker/Dockerfile
FROM ubuntu:24.04

# Common basics
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    nodejs npm \
    curl git jq \
    && rm -rf /var/lib/apt/lists/*

# Install Orchestrator CLI (pre-installed for speed)
COPY services/orchestrator-cli /opt/orchestrator-cli
RUN pip3 install /opt/orchestrator-cli

# Entrypoint
COPY bootstrap.sh /bootstrap.sh
ENTRYPOINT ["/bootstrap.sh"]
```

**bootstrap.sh:**
1.  Читает `SYSTEM_PACKAGES` env var -> `apt-get install`
2.  Читает `BOOTSTRAP_COMMANDS` env var -> `eval`
3.  Генерирует `~/.claude/config.json` и файлы навыков.
4.  Запускает агента.

## Redis Protocol

Стандартный протокол для общения с внешним миром.

| Channel | Type | Purpose |
|---------|------|---------|
| `workers:spawn` | PubSub/Stream | Incoming spawn requests |
| `workers:result` | Stream | Task completion results |
| `workers:events`| Stream | Live updates from agent |

### Spawn Request Payload
```json
{
  "request_id": "req_123",
  "worker_config_id": "po_claude_v1", 
  // OR inline config for extreme flexibility
  "worker_config_override": { ... }, 
  
  "task": {
    "user_id": "user_1",
    "prompt": "Check project status",
    "session_id": "sess_abc"
  }
}
```
