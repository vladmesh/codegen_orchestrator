# 🔧 Анализ рефакторинга проекта Codegen Orchestrator

> **Дата анализа:** 2025-12-25  
> **Версия:** 1.0

---

## 📋 Краткое резюме

Проект имеет хорошую архитектурную основу, но накопил технический долг:
- **~2800 строк** захардкоженной логики в node-файлах
- **~657 строк** в монолитном `tools/database.py`
- **Дублирование** паттерна `execute_tools` в 5 из 6 nodes
- **Отсутствие** Pydantic-схем для валидации данных между агентами
- **Захардкоженные** промпты, модели LLM и конфигурации

---

## 🎯 Приоритеты рефакторинга

| Приоритет | Область | Влияние | Сложность | Статус |
|-----------|---------|---------|-----------|--------|
| 🔴 P0 | Вынос промптов в базу | Высокое | Средняя | ✅ Готово |
| 🔴 P0 | Абстракция `execute_tools` | Высокое | Низкая | |
| 🟠 P1 | Разбиение `tools/database.py` | Среднее | Средняя | |
| 🟠 P1 | Pydantic-схемы для State | Среднее | Средняя | |
| 🟡 P2 | Базовый класс для Nodes | Среднее | Средняя | |
| 🟡 P2 | Конфигурация LLM моделей | Низкое | Низкая | ✅ Готово |
| 🟢 P3 | Рефакторинг provisioner | Низкое | Высокая | |

---

## 🔴 P0: Критические улучшения

### 1. Вынос промптов в базу данных

**Проблема:** Промпты захардкожены в каждом node-файле:

```python
# product_owner.py (lines 23-55)
SYSTEM_PROMPT = """You are the Product Owner (PO) for the codegen orchestrator...

# architect.py (lines 20-72)  
SYSTEM_PROMPT = """You are Architect, the project structuring agent...

# zavhoz.py (lines 39-68)
SYSTEM_PROMPT = """You are Zavhoz, the infrastructure manager...

# brainstorm.py (lines 12-45)
SYSTEM_PROMPT = """You are Brainstorm, the first agent...
```

**Решение:**

#### [NEW] `services/api/src/models/agent_config.py`
```python
class AgentConfig(Base):
    __tablename__ = "agent_configs"
    
    id: Mapped[str] = mapped_column(String(50), primary_key=True)  # "product_owner", "architect"
    name: Mapped[str] = mapped_column(String(100))
    system_prompt: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(100), default="gpt-4o")
    temperature: Mapped[float] = mapped_column(Float, default=0.0)
    tools: Mapped[list] = mapped_column(JSON, default=[])  # Tool names to bind
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, onupdate=func.now())
```

#### [NEW] `services/api/src/routers/agent_configs.py`
```python
@router.get("/{agent_id}")
async def get_agent_config(agent_id: str) -> AgentConfigRead:
    ...

@router.patch("/{agent_id}")
async def update_agent_config(agent_id: str, updates: AgentConfigUpdate):
    ...
```

**Миграция данных:** Seed-скрипт для начальных промптов.

---

### 2. Абстракция паттерна `execute_tools`

**Проблема:** Дублированный код в 5 файлах (~150 строк × 5 = 750 строк):

```python
# Повторяется в: product_owner.py, architect.py, zavhoz.py, brainstorm.py, devops.py

async def execute_tools(state: dict) -> dict:
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    tool_results = []
    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_func = tools_map.get(tool_name)
        # ... ~50 lines of duplicated logic
```

**Решение:**

#### [NEW] `services/langgraph/src/nodes/base.py`
```python
from abc import ABC, abstractmethod
from typing import Callable
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool


class BaseAgentNode(ABC):
    """Base class for all agent nodes with common tool execution logic."""
    
    def __init__(self, agent_id: str, tools: list[BaseTool]):
        self.agent_id = agent_id
        self.tools = tools
        self.tools_map = {tool.name: tool for tool in tools}
        self._llm = None
    
    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Get system prompt (from DB or config)."""
        pass
    
    async def get_llm_with_tools(self):
        """Get configured LLM with bound tools."""
        if self._llm is None:
            config = await self._fetch_config()
            llm = ChatOpenAI(
                model=config.get("model_name", "gpt-4o"),
                temperature=config.get("temperature", 0),
            )
            self._llm = llm.bind_tools(self.tools)
        return self._llm
    
    async def execute_tools(self, state: dict) -> dict:
        """Generic tool execution with error handling."""
        messages = state.get("messages", [])
        last_message = messages[-1]

        if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return {"messages": []}

        tool_results = []
        state_updates = {}

        for tool_call in last_message.tool_calls:
            result = await self._execute_single_tool(tool_call, state)
            tool_results.append(result["message"])
            state_updates.update(result.get("state_updates", {}))

        return {"messages": tool_results, **state_updates}
    
    async def _execute_single_tool(
        self, tool_call: dict, state: dict
    ) -> dict:
        """Execute a single tool call with error handling."""
        tool_name = tool_call["name"]
        tool_func = self.tools_map.get(tool_name)

        if not tool_func:
            return {
                "message": ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            }

        try:
            result = await tool_func.ainvoke(tool_call["args"])
            return {
                "message": ToolMessage(
                    content=f"Result: {result}",
                    tool_call_id=tool_call["id"],
                ),
                "state_updates": self.handle_tool_result(tool_name, result, state),
            }
        except Exception as e:
            return {
                "message": ToolMessage(
                    content=f"Error: {e!s}",
                    tool_call_id=tool_call["id"],
                )
            }
    
    def handle_tool_result(
        self, tool_name: str, result: Any, state: dict
    ) -> dict:
        """Override in subclasses to handle specific tool results."""
        return {}
```

**Рефакторинг nodes:**

```python
# Было (product_owner.py - 379 строк):
async def run(state: dict) -> dict: ...
async def execute_tools(state: dict) -> dict: ...

# Стало (~100 строк):
class ProductOwnerNode(BaseAgentNode):
    def handle_tool_result(self, tool_name, result, state):
        if tool_name == "create_project_intent":
            return {"po_intent": result.get("intent")}
        # ...

product_owner = ProductOwnerNode("product_owner", tools)
run = product_owner.run
execute_tools = product_owner.execute_tools
```

---

## 🟠 P1: Важные улучшения

### 3. Разбиение `tools/database.py` (657 строк)

**Проблема:** Монолитный файл с 20+ tools разной ответственности:

```
tools/database.py (657 lines)
├── Project tools: create_project, list_projects, get_project_status, ...
├── Server tools: list_managed_servers, find_suitable_server, ...
├── Port tools: allocate_port, get_next_available_port
├── Incident tools: create_incident, list_active_incidents, ...
├── Activation tools: activate_project, inspect_repository, ...
└── Helper functions: _parse_env_example
```

**Решение:**

```
services/langgraph/src/tools/
├── __init__.py          # Re-exports all tools
├── base.py              # APIClient, base helpers
├── projects.py          # create_project, list_projects, get_project_status, set_project_maintenance
├── servers.py           # list_managed_servers, find_suitable_server, get_server_info
├── ports.py             # allocate_port, get_next_available_port
├── incidents.py         # create_incident, list_active_incidents, get_services_on_server
├── activation.py        # activate_project, inspect_repository, save_project_secret, check_ready_to_deploy
└── resources.py         # list_resource_inventory, create_service_deployment
```

**Базовый API-клиент:**

```python
# tools/base.py
class InternalAPIClient:
    """Singleton async HTTP client for internal API."""
    
    def __init__(self):
        self.base_url = os.getenv("API_URL", "http://api:8000")
        self._client: httpx.AsyncClient | None = None
    
    async def get(self, path: str, **kwargs) -> dict:
        client = await self._get_client()
        resp = await client.get(f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()
    
    async def post(self, path: str, **kwargs) -> dict:
        client = await self._get_client()
        resp = await client.post(f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()
    
    # ... patch, delete, etc.

api_client = InternalAPIClient()
```

---

### 4. Pydantic-схемы для OrchestratorState

**Проблема:** `TypedDict` не валидирует данные в runtime:

```python
# graph.py (lines 15-56)
class OrchestratorState(TypedDict):
    messages: Annotated[list, add_messages]
    current_project: str | None
    project_spec: dict | None  # ← Нет валидации структуры
    allocated_resources: dict   # ← Нет типизации значений
    repo_info: dict | None      # ← Неизвестные поля
    # ... 20 полей без валидации
```

**Решение:**

#### [MODIFY] `services/langgraph/src/schemas.py`

```python
from pydantic import BaseModel, Field
from typing import Literal


class RepoInfo(BaseModel):
    """Repository information from GitHub."""
    full_name: str
    html_url: str
    clone_url: str


class AllocatedResource(BaseModel):
    """Single allocated resource (port on server)."""
    server_handle: str
    server_ip: str
    port: int
    service_name: str


class ProjectIntent(BaseModel):
    """Intent from Product Owner."""
    intent: Literal["new_project", "update_project", "deploy", "maintenance"]
    summary: str | None = None
    project_id: str | None = None


class TestResults(BaseModel):
    """Test execution results."""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    output: str = ""


class OrchestratorStateModel(BaseModel):
    """Validated orchestrator state for debugging and serialization."""
    
    # Core
    current_project: str | None = None
    project_spec: ProjectSpec | None = None
    project_intent: ProjectIntent | None = None
    po_intent: Literal["new_project", "maintenance", "deploy"] | None = None
    
    # Resources
    allocated_resources: dict[str, AllocatedResource] = Field(default_factory=dict)
    
    # Repository
    repo_info: RepoInfo | None = None
    project_complexity: Literal["simple", "complex"] | None = None
    architect_complete: bool = False
    
    # Engineering
    engineering_status: Literal["idle", "working", "done", "blocked"] = "idle"
    review_feedback: str | None = None
    engineering_iterations: int = 0
    test_results: TestResults | None = None
    
    # Human-in-the-loop
    needs_human_approval: bool = False
    human_approval_reason: str | None = None
    
    # Provisioning
    server_to_provision: str | None = None
    is_incident_recovery: bool = False
    
    # Status
    current_agent: str = "unknown"
    errors: list[str] = Field(default_factory=list)
    deployed_url: str | None = None

    class Config:
        extra = "forbid"  # Catch typos in state keys
```

**Интеграция:** Валидация на входе/выходе каждого node.

---

## 🟡 P2: Рекомендуемые улучшения

### 5. Конфигурация LLM моделей

**Проблема:** Модели захардкожены:

```python
# product_owner.py:59
llm = ChatOpenAI(model="gpt-4o", temperature=0.2)

# architect.py:86
llm = ChatOpenAI(model="gpt-4o", temperature=0)

# brainstorm.py:48
llm = ChatOpenAI(model="gpt-4o", temperature=0.7)
```

**Решение:** Часть таблицы `agent_configs` (см. P0.1).

---

### 6. Выделение форматтеров ответов

**Проблема:** Логика форматирования UI mixed с business logic:

```python
# product_owner.py (lines 152-166) - форматирование инцидентов
# product_owner.py (lines 178-186) - форматирование проектов  
# product_owner.py (lines 188-206) - форматирование серверов
```

**Решение:**

#### [NEW] `services/langgraph/src/formatters/`
```python
# formatters/incidents.py
def format_incidents_list(incidents: list[dict]) -> str:
    if not incidents:
        return ""
    lines = ["🚨 **Активные инциденты:**"]
    for inc in incidents:
        # ... formatting logic
    return "\n".join(lines)

# formatters/servers.py
def format_servers_list(servers: list[dict]) -> str:
    ...

# formatters/__init__.py
from .incidents import format_incidents_list
from .servers import format_servers_list
from .projects import format_projects_list
```

---

## 🟢 P3: Дополнительные улучшения

### 7. Рефакторинг Provisioner (452 строки)

**Проблема:** `provisioner/node.py` слишком большой, смешивает orchestration и business logic.

**Решение:** Уже частично выполнено (выделены `ansible_runner.py`, `api_client.py`, `incidents.py`, `recovery.py`, `ssh.py`).

Дополнительно можно:
- Выделить `password_reset_flow.py`
- Выделить `reinstall_flow.py`
- Добавить State Machine для provisioning states

---

### 8. DRY в routing functions

**Проблема:** Похожая логика в routing functions:

```python
# graph.py - повторяется 4 раза
def route_after_X(state):
    messages = state.get("messages", [])
    if not messages:
        return END
    last_message = messages[-1]
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "X_tools"
    ...
```

**Решение:**

```python
def has_tool_calls(state: dict) -> bool:
    messages = state.get("messages", [])
    if not messages:
        return False
    last = messages[-1]
    return hasattr(last, "tool_calls") and bool(last.tool_calls)

def route_after_agent(agent_name: str, next_routes: dict[str, str]):
    def router(state):
        if has_tool_calls(state):
            return f"{agent_name}_tools"
        for condition, target in next_routes.items():
            if check_condition(state, condition):
                return target
        return END
    return router
```

---

### 9. Кеширование агентских конфигов

**Проблема:** Каждый вызов агента будет ходить в БД за промптом.

**Решение:**

```python
# services/langgraph/src/config/agent_config_cache.py
from cachetools import TTLCache
import asyncio

class AgentConfigCache:
    def __init__(self, ttl_seconds: int = 60):
        self._cache = TTLCache(maxsize=100, ttl=ttl_seconds)
        self._lock = asyncio.Lock()
    
    async def get(self, agent_id: str) -> dict:
        if agent_id in self._cache:
            return self._cache[agent_id]
        
        async with self._lock:
            if agent_id in self._cache:
                return self._cache[agent_id]
            
            config = await self._fetch_from_api(agent_id)
            self._cache[agent_id] = config
            return config
    
    def invalidate(self, agent_id: str | None = None):
        if agent_id:
            self._cache.pop(agent_id, None)
        else:
            self._cache.clear()

agent_config_cache = AgentConfigCache()
```

---

## 📊 Метрики до/после

| Метрика | До | После (ожидаемо) |
|---------|-----|------------------|
| Строк в nodes/ | ~1800 | ~800 |
| Строк в tools/database.py | 657 | 0 (разбит) |
| Дублирование execute_tools | 750 | 0 |
| Pydantic-схемы агентов | 1 | 8 |
| Файлов с >300 строк | 5 | 1 |
| Захардкоженных промптов | 5 | 0 |

---

## 🗓 Roadmap реализации

### Фаза 1: Инфраструктура ✅ ЗАВЕРШЕНО
- [x] Создать модель `AgentConfig` и миграцию
- [x] Создать API endpoints для agent configs
- [x] Seed initial prompts

### Фаза 2: Base Node ✅ ЗАВЕРШЕНО
- [x] Создать `BaseAgentNode` класс
- [x] Рефакторинг `brainstorm.py` как proof-of-concept
- [x] Добавить тесты

### Фаза 3: Миграция nodes ✅ ЗАВЕРШЕНО
- [x] Мигрировать `zavhoz.py`
- [x] Мигрировать `architect.py`
- [x] Мигрировать `product_owner.py`
- [x] `devops.py` - без промпта (прямой Ansible)

### Фаза 4: Tools reorganization (1-2 дня)
- [ ] Разбить `tools/database.py`
- [ ] Создать `InternalAPIClient`
- [ ] Обновить импорты

### Фаза 5: Pydantic schemas (1-2 дня)
- [ ] Добавить схемы состояния
- [ ] Добавить валидацию в nodes
- [ ] Тесты

---

## ⚠️ Риски

1. **Breaking changes:** Изменение структуры nodes может сломать graph routing.
2. **Migration complexity:** Seed-скрипт для промптов требует аккуратности.
3. **Performance:** Кеширование конфигов критично для latency.

---

## 📚 Связанные документы

- [ARCHITECTURE.md](../ARCHITECTURE.md)
- [NODES.md](./NODES.md)
- [project_lifecycle.md](./project_lifecycle.md)
