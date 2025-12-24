# Архитектура

## Обзор

Codegen Orchestrator — это мультиагентная система на базе LangGraph, где каждый агент является узлом графа со своими инструментами. Агенты могут вызывать друг друга нелинейно для решения сложных задач.

## Технический стек

| Компонент | Технология |
|-----------|------------|
| Оркестрация | LangGraph |
| LLM | OpenAI / Anthropic (через Завхоза) |
| Интерфейс | Telegram Bot |
| Кодогенерация | service-template (Copier) |
| Инфраструктура | prod_infra (Ansible) |
| Хранение состояния | PostgreSQL (TBD) |
| Секреты | SOPS + AGE (MVP) |

## State Schema

Глобальное состояние графа, доступное всем агентам:

```python
from typing import TypedDict, Annotated
from langgraph.graph.message import add_messages

class OrchestratorState(TypedDict):
    # Сообщения (история диалога)
    messages: Annotated[list, add_messages]
    
    # Текущий проект
    current_project: str | None
    project_spec: dict | None  # ТЗ от Брейнсторма
    
    # Ресурсы
    allocated_resources: dict  # {resource_type: resource_id}
    
    # Статус
    current_agent: str
    pending_actions: list[str]
    errors: list[str]
    
    # Результаты
    deployed_url: str | None
    test_results: dict | None
```

## Граф

```python
from langgraph.graph import StateGraph, END

graph = StateGraph(OrchestratorState)

# Добавляем узлы (агентов)
graph.add_node("brainstorm", brainstorm_agent)
graph.add_node("architect", architect_agent)
graph.add_node("developer", developer_agent)
graph.add_node("tester", tester_agent)
graph.add_node("devops", devops_agent)
graph.add_node("zavhoz", zavhoz_agent)
graph.add_node("documentator", documentator_agent)

# Добавляем рёбра
graph.set_entry_point("brainstorm")

# Условные переходы (пример)
graph.add_conditional_edges(
    "brainstorm",
    route_after_brainstorm,
    {
        "architect": "architect",
        "clarify": "brainstorm",  # нужно уточнение
        "end": END
    }
)

# ... остальные рёбра
```

## Внешние зависимости

### service-template

Используется Архитектором для генерации проектов:

```python
# Генерация нового проекта
subprocess.run([
    "copier", "copy",
    "gh:vladmesh/service-template", target_dir,
    "--data", f"project_name={name}",
    "--data", f"modules={modules}"
])

# Генерация кода из спецификаций
subprocess.run(["make", "generate-from-spec"], cwd=target_dir)
```

### prod_infra

Используется DevOps для деплоя:

```python
# Bootstrap нового сервера
subprocess.run([
    "ansible-playbook",
    "-i", "inventory/prod.ini",
    "playbooks/bootstrap.yml"
], cwd="/path/to/prod_infra/ansible")

# Деплой с обновлением services.yml
# 1. Обновить services.yml
# 2. ansible-playbook playbooks/site.yml
```

### Завхоз (Resource Manager)

Завхоз — узел LangGraph, управляющий ресурсами с изоляцией секретов от LLM.

#### Принцип: LLM никогда не видит секреты

```
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph State                          │
│  (это видит LLM)                                           │
│                                                             │
│  allocated_resources: {                                    │
│      "telegram_bot": "handle_abc123",  ← handle, не токен  │
│      "server": "prod_vps_1"            ← имя, не IP/SSH    │
│  }                                                          │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  Завхоз (узел LangGraph)                    │
│                                                             │
│  LLM-часть:                                                │
│  - Решает КАКОЙ ресурс нужен                               │
│  - Возвращает handle/имя в state                           │
│                                                             │
│  Python-часть (вне видимости LLM):                         │
│  - Читает реальные секреты из storage                      │
│  - Передаёт в subprocess через env vars                    │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                   Encrypted Storage                         │
│  (SOPS + YAML, позже PostgreSQL)                           │
│                                                             │
│  telegram_bots:                                            │
│    handle_abc123:                                          │
│      name: "@weather_bot"                                  │
│      token: "123456:ABC..."  ← реальный токен             │
└─────────────────────────────────────────────────────────────┘
```

#### Пример: деплой использует секреты, но LLM их не видит

```python
@tool
def deploy_to_server(server_handle: str, project_path: str):
    """Deploy project to server. LLM calls this with handle only."""
    # Python-код читает секреты напрямую, минуя LLM
    server = secret_storage.get_server(server_handle)
    
    subprocess.run(
        ["ansible-playbook", "playbooks/site.yml"],
        env={
            "ANSIBLE_HOST": server.host,        # LLM не видит
            "ANSIBLE_SSH_KEY": server.ssh_key,  # LLM не видит
        }
    )
    return "Deployed successfully"  # ← только это в контекст
```

#### Хранение секретов (MVP)

SOPS + AGE для шифрования YAML файла:

```yaml
# secrets.yaml (зашифрован SOPS)
telegram_bots:
    handle_abc123:
        name: "@weather_bot"
        token: ENC[AES256_GCM,data:...,iv:...,tag:...]

servers:
    prod_vps_1:
        host: ENC[AES256_GCM,data:...,iv:...,tag:...]
        ssh_key: ENC[AES256_GCM,data:...,iv:...,tag:...]

api_keys:
    openai:
        key: ENC[AES256_GCM,data:...,iv:...,tag:...]
```

```bash
# Расшифровка при старте оркестратора
export SOPS_AGE_KEY_FILE=~/.age/key.txt
sops -d secrets.yaml > /tmp/secrets.yaml
```

#### Что хранит Завхоз

| Категория | Handle пример | Реальные данные |
|-----------|---------------|-----------------|
| Telegram боты | `handle_abc123` | token, username |
| Серверы | `prod_vps_1` | IP, SSH key |
| API ключи | `openai_main` | API key |
| Домены | `example.com` | Cloudflare credentials |

#### Управление Инфраструктурой (Server Management)

Система поддерживает гибридную инфраструктуру, синхронизируемую с провайдером (Time4VPS).

1.  **Source of Truth**: База данных (`api` сервис).
    *   Фоновый worker (`server_sync.py`) каждую минуту опрашивает Time4VPS API.
    *   Новые сервера автоматически добавляются со статусом `discovered`.
    *   Удаленные сервера помечаются как `missing`.

2.  **Ghost Servers & Filtering**:
    *   Сервера, которые нужно игнорировать (личные машины разработчиков), прописываются в `GHOST_SERVERS`.
    *   В базе они помечаются как `is_managed=False`.
    *   Zavhoz использует инструмент `list_managed_servers`, который возвращает только `is_managed=True`.

#### GitHub App & Secrets

Для работы с GitHub (создание репозиториев, управление workflows) используется GitHub App.

| Secret Name | Описание | Где хранится |
|-------------|----------|--------------|
| `GH_APP_ID` | App ID приложения Project-Factory-Keeper | GitHub Secrets |
| `GH_APP_PRIVATE_KEY` | Private Key (.pem) для подписи JWT | GitHub Secrets |

**Локальная разработка:**
- `GITHUB_APP_ID` → `.env`
- Private Key → `~/.gemini/keys/github_app.pem` (mount в docker-compose)

**Production:**
- Secrets записываются на сервер через CI/CD workflow
- Путь на проде: `/opt/secrets/github_app.pem`

## Persistence

### Checkpointing

LangGraph поддерживает checkpointing для сохранения состояния:

```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver(connection_string)
app = graph.compile(checkpointer=checkpointer)
```

### Threads

Каждый проект = отдельный thread:

```python
config = {"configurable": {"thread_id": project_id}}
result = await app.ainvoke(state, config)
```

## Телеграм интеграция

```python
from telegram.ext import Application, CommandHandler, MessageHandler

async def handle_message(update, context):
    user_message = update.message.text
    project_id = get_or_create_project(update.effective_user.id)
    
    config = {"configurable": {"thread_id": project_id}}
    result = await orchestrator.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
        config
    )
    
    await update.message.reply_text(result["messages"][-1].content)
```

## Внешние Coding Agents

Для задач разработки используем production-ready инструменты вместо написания своих агентов.

### Claude Code (Anthropic)

CLI-инструмент для agentic coding. Понимает весь codebase, редактирует файлы, запускает команды.

```bash
# Установка
npm install -g @anthropic-ai/claude-code

# Использование
claude -p "Implement user registration endpoint"

# Pipe
cat error.log | claude -p "Fix this error"
```

**Контекст:** Использует `CLAUDE.md` файлы (аналог нашего `AGENTS.md`).

**Цена:** Pro/Max подписка (~$20-100/мес), дешевле чем API.

### Factory.ai Droid

Автономный coding agent с уровнями автономности.

```bash
# Интерактивный режим
droid

# Single-shot (для автоматизации)
droid exec "Implement feature X" --autonomy high

# Из файла
droid exec --prompt-file task.md
```

**Autonomy levels:** low (много подтверждений), medium, high (полная автономия).

### Маппинг на узлы графа

| Узел | Инструмент | Почему |
|------|------------|--------|
| **Архитектор** | Claude Code | Понимает codebase, генерит структуру |
| **Разработчик** | Droid (high autonomy) | Автономная реализация фич |
| **Тестировщик** | Claude Code / Droid | Пишут и запускают тесты |
| **DevOps** | Custom (Ansible wrapper) | Специфичная задача |
| **Завхоз** | LangGraph native | Доступ к секретам |

### Интеграция в LangGraph

```python
import subprocess

async def developer_node(state: dict) -> dict:
    """Developer node using external coding agent."""
    task = state["current_task"]
    project_path = state["project_path"]
    
    # Записываем контекст для агента
    Path(f"{project_path}/TASK.md").write_text(task["description"])
    
    # Вызываем Claude Code
    result = subprocess.run(
        ["claude", "-p", "Read TASK.md and implement. Run tests."],
        cwd=project_path,
        capture_output=True,
        text=True
    )
    
    return {
        "messages": [AIMessage(content=result.stdout)],
        "current_agent": "developer"
    }
```

## Параллельные Workers

Для независимых задач запускаем отдельные контейнеры с coding agents.

### Архитектура

```
┌─────────────────────────────────────────────────────┐
│                 LangGraph Orchestrator              │
│  tasks = [{scope: "frontend"}, {scope: "backend"}] │
└─────────────────────────────────────────────────────┘
                         │
         ┌───────────────┴───────────────┐
         ▼                               ▼
┌──────────────────┐            ┌──────────────────┐
│  Worker (task_1) │            │  Worker (task_2) │
│  - git clone     │            │  - git clone     │
│  - claude/droid  │            │  - claude/droid  │
│  - docker compose│            │  - docker compose│
│  - gh pr create  │            │  - gh pr create  │
└──────────────────┘            └──────────────────┘
         │                               │
         └───────────────┬───────────────┘
                         ▼
               ┌──────────────────┐
               │   Reviewer Agent  │
               │   gh pr review    │
               │   gh pr merge     │
               └──────────────────┘
```

### Docker-in-Docker с Sysbox

Для запуска `docker compose` внутри контейнера используем [Sysbox](https://github.com/nestybox/sysbox) — безопасный Docker-in-Docker без privileged mode.

**Установка на хост:**
```bash
wget https://downloads.nestybox.com/sysbox/releases/v0.6.4/sysbox-ce_0.6.4-0.linux_amd64.deb
sudo dpkg -i sysbox-ce_0.6.4-0.linux_amd64.deb
```

**Запуск worker контейнера:**
```bash
docker run --runtime=sysbox-runc -it --rm \
    -e GITHUB_TOKEN=... \
    -e ANTHROPIC_API_KEY=... \
    coding-worker:latest
```

**Внутри контейнера доступно:**
- Полноценный Docker daemon
- `git clone`, `git push`
- `docker compose up -d`
- `gh pr create`

### Worker Dockerfile

```dockerfile
FROM nestybox/ubuntu-jammy-systemd-docker

RUN apt-get update && apt-get install -y git curl python3 python3-pip

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh

# Claude Code
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /workspace
```

### Запуск параллельных workers

```python
async def parallel_developer_node(state: dict) -> dict:
    """Run multiple coding tasks in parallel."""
    tasks = state["pending_tasks"]
    
    # Запускаем всех воркеров параллельно
    results = await asyncio.gather(*[
        spawn_sysbox_worker(task)
        for task in tasks
    ])
    
    return {
        "pending_prs": [parse_pr_url(r) for r in results],
        "pending_tasks": []
    }

# Service descriptions for workers
# - **Tooling (`services/tooling`)**: Standard utility container for linting and formatting.
# - **Infrastructure (`services/infrastructure`)**: Contains Ansible playbooks and roles for server configuration and deployment. This is the toolbox for the DevOps agent.

async def spawn_sysbox_worker(task: dict) -> str:
    """Spawn Sysbox container for a task."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm",
        "--runtime=sysbox-runc",
        "-e", f"TASK={task['description']}",
        "-e", f"REPO={task['repo']}",
        "coding-worker:latest",
        "/scripts/execute_task.sh",
        stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    return stdout.decode()
```

### Reviewer Agent

```python
async def reviewer_node(state: dict) -> dict:
    """Review and merge PRs."""
    for pr_url in state["pending_prs"]:
        diff = subprocess.run(
            ["gh", "pr", "diff", pr_url],
            capture_output=True, text=True
        ).stdout
        
        review = await review_with_llm(diff)
        
        if review["approved"]:
            subprocess.run(["gh", "pr", "merge", pr_url, "--squash"])
        else:
            subprocess.run([
                "gh", "pr", "comment", pr_url,
                "--body", review["feedback"]
            ])
    
    return {"messages": [...]}
```

### Ограничения параллельных workers

| Аспект | Ограничение |
|--------|-------------|
| RAM | ~2-4GB на worker (Docker daemon + контейнеры) |
| Startup | Docker daemon стартует 5-10 сек |
| Disk | Образы качаются в каждый worker (кэшировать через volumes) |
| GitHub API | Rate limits — добавить throttling |

## Мониторинг и отладка

### LangSmith

Для трейсинга всех вызовов агентов:

```bash
export LANGCHAIN_TRACING_V2=true
export LANGCHAIN_API_KEY=...
```

### Логирование

Каждый агент логирует:
- Входные данные
- Принятые решения
- Вызванные инструменты
- Результаты

## Открытые вопросы

### Решено

1. ~~**Ресурсница**: отдельный сервис или часть оркестратора?~~ → **Узел LangGraph** с изоляцией секретов
2. ~~**Хранение секретов**~~ → **SOPS + YAML** (MVP), позже PostgreSQL
3. ~~**Coding agents**: писать свои или использовать готовые?~~ → **Claude Code / Factory Droid**
4. ~~**Docker-in-Docker для тестов**~~ → **Sysbox** (безопасный nested Docker)

### В работе

5. **Формат спеков**: как передавать ТЗ между агентами?
6. **Error handling**: что делать когда агент застрял?
7. **Human escalation**: когда просить помощи у человека?
8. **Cost tracking**: как отслеживать расходы на LLM?
9. **Merge conflicts**: как разрешать при параллельных PR?
10. **Worker image caching**: как ускорить startup?
