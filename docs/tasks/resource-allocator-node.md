# Resource Allocator Node for DevOps Subgraph

> **Приоритет**: High
> **Оценка**: ~3 часа
> **Статус**: Planning

## Проблема

Deploy падает с ошибкой "No resources allocated for project":

```
deploy-worker-1  | deploy_job_started  project_id=hello-world-bot
deploy-worker-1  | HTTP GET /api/allocations/?project_id=hello-world-bot
# Возвращает пустой список → fail
```

### Текущая архитектура

```
Main Graph: START → analyst → zavhoz (LLM) → engineering → devops → END
                                   ↓
                         allocate_port tool (LLM вызывает)

Deploy Worker: напрямую запускает DevOps subgraph
               └─ Zavhoz пропускается!
```

**Zavhoz** — LLM-нода в main graph, но:
1. `deploy_worker.py` вызывает DevOps subgraph напрямую
2. Zavhoz не вызывается → allocations не создаются
3. DevOps subgraph падает

## Решение

Добавить **функциональную ноду ResourceAllocator** в начало DevOps subgraph.

### Новая архитектура

```
DevOps Subgraph:
  START → ResourceAllocator → EnvAnalyzer → SecretResolver → Deployer → END
              ↓
    (детерминированная логика, без LLM)
```

### Реализация

#### 1. Новый файл: `services/langgraph/src/nodes/resource_allocator.py`

```python
"""Resource Allocator - functional node for port/server allocation."""

import structlog

from ..schemas.api_types import AllocationInfo, ServerInfo
from ..tools.ports import allocate_port, get_next_available_port
from ..tools.servers import find_suitable_server
from .base import FunctionalNode

logger = structlog.get_logger()


class ResourceAllocatorNode(FunctionalNode):
    """Allocate server resources for a project before deployment.

    This is a FUNCTIONAL node (no LLM) - deterministic logic only.
    """

    def __init__(self):
        super().__init__(node_id="resource_allocator")

    async def run(self, state: dict) -> dict:
        """Allocate ports for each module in project."""
        project_id = state.get("project_id")
        project_spec = state.get("project_spec") or {}

        if not project_id:
            return {
                "errors": state.get("errors", []) + ["No project_id provided"],
            }

        # Check if already allocated
        existing = state.get("allocated_resources", {})
        if existing:
            logger.info(
                "resources_already_allocated",
                project_id=project_id,
                count=len(existing),
            )
            return {}  # Already done

        # Determine modules from project config
        config = project_spec.get("config", {})
        modules = config.get("modules", ["backend"])

        # Estimate resources (simple heuristic)
        min_ram_mb = config.get("estimated_ram_mb", 512)

        logger.info(
            "resource_allocation_start",
            project_id=project_id,
            modules=modules,
            min_ram_mb=min_ram_mb,
        )

        try:
            # 1. Find suitable server
            server = await find_suitable_server.ainvoke({
                "min_ram_mb": min_ram_mb,
                "min_disk_mb": 1024,
            })

            if not server:
                return {
                    "errors": state.get("errors", []) + [
                        "No suitable server found with enough resources"
                    ],
                }

            server_handle = server.handle

            # 2. Allocate port for each module
            allocated = {}
            for module in modules:
                # Get next available port
                port = await get_next_available_port.ainvoke({
                    "server_handle": server_handle,
                    "start_port": 8000,
                })

                # Allocate it
                allocation = await allocate_port.ainvoke({
                    "server_handle": server_handle,
                    "port": port,
                    "service_name": module,
                    "project_id": project_id,
                })

                port_key = f"{server_handle}:{port}"
                allocated[port_key] = allocation.model_dump()

                logger.info(
                    "port_allocated",
                    project_id=project_id,
                    module=module,
                    server=server_handle,
                    port=port,
                )

            return {"allocated_resources": allocated}

        except Exception as e:
            logger.error(
                "resource_allocation_failed",
                project_id=project_id,
                error=str(e),
            )
            return {
                "errors": state.get("errors", []) + [f"Resource allocation failed: {e}"],
            }


resource_allocator_node = ResourceAllocatorNode()
```

#### 2. Обновить DevOps subgraph: `services/langgraph/src/subgraphs/devops.py`

```python
# Добавить импорт
from ..nodes.resource_allocator import resource_allocator_node

# В create_devops_subgraph():
graph.add_node("resource_allocator", resource_allocator_node.run)

# Изменить edges:
graph.add_edge(START, "resource_allocator")
graph.add_edge("resource_allocator", "env_analyzer")
# ... остальное без изменений
```

#### 3. Обновить DevOpsState

Добавить `project_id` если его нет:

```python
class DevOpsState(TypedDict):
    project_id: str  # <-- убедиться что есть
    project_spec: dict | None
    allocated_resources: dict
    # ...
```

## Acceptance Criteria

- [ ] DevOps subgraph начинается с ResourceAllocator
- [ ] Allocations создаются автоматически перед деплоем
- [ ] Если нет подходящего сервера → понятная ошибка
- [ ] Если allocations уже есть → не дублируются
- [ ] Нет LLM вызовов в ResourceAllocator (чисто функциональная логика)

## Файлы для изменения

1. **Создать**: `services/langgraph/src/nodes/resource_allocator.py`
2. **Изменить**: `services/langgraph/src/subgraphs/devops.py`
3. **Проверить**: `services/langgraph/src/schemas/devops.py` (DevOpsState)
4. **Обновить**: `services/langgraph/src/nodes/__init__.py`

## Зависимости

- Требует работающий `servers` в БД со статусом `ready` или `in_use`
- Требует `server_sync.py` для заполнения серверов

## Тестирование

```bash
# Проверить что серверы есть
docker exec codegen_orchestrator-db-1 psql -U postgres -d orchestrator \
  -c "SELECT handle, status, capacity_ram_mb FROM servers WHERE status IN ('ready', 'in_use');"

# Если пусто - нужно добавить тестовый сервер или дождаться sync
```
