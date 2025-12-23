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

### В работе

3. **Формат спеков**: как передавать ТЗ между агентами?
4. **Error handling**: что делать когда агент застрял?
5. **Human escalation**: когда просить помощи у человека?
6. **Cost tracking**: как отслеживать расходы на LLM?

