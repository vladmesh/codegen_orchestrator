# Упрощение Engineering Subgraph для MVP

> **Статус**: Complete ✅
> **Создано**: 2026-01-09
> **Завершено**: 2026-01-09

## Проблема

### Текущее состояние

Engineering Subgraph состоит из 4 нод:

```
START → Architect (LLM) → architect_tools → Preparer (container) → Developer (container) → Tester → END
```

**Проблемы:**

1. **Preparer не работает** — использует Redis Pub/Sub (`preparer:spawn`), но нет consumer'а
2. **Избыточная сложность** — 3 контейнера для одной задачи (Architect в LangGraph, Preparer container, Developer container)
3. **Непоследовательность** — Preparer use отдельный механизм спауна, Developer использует workers-spawner
4. **Overhead** — переключение между контейнерами теряет контекст

### Целевое состояние (MVP)

```
START → Developer (container с copier) → Tester (заглушка) → END
```

- **Developer** = Architect + Preparer + Developer (один Claude agent)
- **Tester** = заглушка (всегда passed=True)
- Один контейнер на весь engineering цикл
- Claude сам решает: copier → код → commit → push

---

## Подход

### Принципы

1. **workers-spawner — единая точка спауна контейнеров**
2. **Capabilities определяют что установить** — copier добавляется через CapabilityType.COPIER
3. **Claude agent делает всё** — архитектура, scaffolding, код
4. **Минимум изменений в universal-worker** — динамическая установка через INSTALL_COMMANDS

### Архитектура после изменений

```
┌─────────────────────────────────────────────────────────────────┐
│                    Engineering Subgraph                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  START                                                           │
│    │                                                             │
│    ▼                                                             │
│  Developer Node                                                  │
│    │                                                             │
│    │  1. Готовит WorkerConfig:                                   │
│    │     - agent: claude-code                                    │
│    │     - capabilities: [git, github, copier]                   │
│    │     - env_vars: {GITHUB_TOKEN: ...}                        │
│    │                                                             │
│    │  2. Спаунит через workers-spawner:                          │
│    │     cli-agent:commands → create                             │
│    │                                                             │
│    │  3. workers-spawner:                                        │
│    │     - docker run universal-worker                           │
│    │     - INSTALL_COMMANDS='["pip install copier"]'             │
│    │     - Контейнер готов                                       │
│    │                                                             │
│    │  4. send_message с задачей:                                 │
│    │     - Склонировать repo                                     │
│    │     - Запустить copier с модулями                           │
│    │     - Написать бизнес-логику                                │
│    │     - Commit + push                                         │
│    │                                                             │
│    │  5. Ждёт ответа → получает commit SHA                       │
│    ▼                                                             │
│  Tester Node (заглушка)                                          │
│    │                                                             │
│    │  → return {passed: True}                                    │
│    ▼                                                             │
│  END                                                             │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## План реализации

### Phase 1: Добавить COPIER capability в workers-spawner

**Цель**: workers-spawner умеет устанавливать copier при создании контейнера

#### 1.1 Добавить CapabilityType.COPIER

**Файл**: `services/workers-spawner/src/workers_spawner/models.py`

```python
class CapabilityType(str, Enum):
    GIT = "git"
    GITHUB = "github"
    CURL = "curl"
    NODE = "node"
    PYTHON = "python"
    DOCKER = "docker"
    COPIER = "copier"  # NEW
```

#### 1.2 Обработать COPIER в container_service

**Файл**: `services/workers-spawner/src/workers_spawner/container_service.py`

В методе `create_container`, добавить в `INSTALL_COMMANDS`:

```python
# Existing install_commands logic
install_commands = [...]

# Add copier if capability is set
if CapabilityType.COPIER in config.capabilities:
    install_commands.append("pip3 install --break-system-packages copier==9.4.1")
```

#### 1.3 Проверка

```bash
# Тест: создать контейнер с copier capability
# Проверить что copier установлен
docker exec agent-xxx copier --version
```

---

### Phase 2: Упростить Engineering Subgraph

**Цель**: убрать Architect, Preparer, оставить только Developer + Tester

#### 2.1 Создать новый DeveloperNode

**Файл**: `services/langgraph/src/nodes/developer.py`

Переписать `DeveloperNode`:

```python
class DeveloperNode(FunctionalNode):
    """Unified Developer node - handles architecture, scaffolding, and coding."""
    
    async def run(self, state: dict) -> dict:
        """Spawn worker and delegate all engineering work to Claude."""
        
        # 1. Get project spec and repo info
        project_spec = state.get("project_spec") or {}
        
        # 2. Build WorkerConfig with COPIER capability
        config = {
            "name": f"Developer {project_spec.get('name', 'project')}",
            "agent": "claude-code",
            "capabilities": ["git", "github", "copier"],  # COPIER added
            "allowed_tools": ["project"],
            "env_vars": {"GITHUB_TOKEN": await self._get_github_token(...)},
        }
        
        # 3. Build comprehensive task message
        task_message = self._build_task_message(project_spec)
        
        # 4. Spawn via workers-spawner and send message
        result = await request_spawn(...)
        
        # 5. Return result
        return {
            "engineering_status": "done" if result.success else "blocked",
            "commit_sha": result.commit_sha,
            ...
        }
    
    def _build_task_message(self, project_spec: dict) -> str:
        """Build comprehensive task for Claude."""
        return f"""
# Task: Build {project_spec.get('name')}

## Project Specification
{project_spec.get('description', '')}

## Steps
1. Create GitHub repository (if not exists)
2. Clone the repository
3. Run copier to scaffold project structure:
   copier copy gh:vladmesh/service-template . --data project_name={name} --data modules={modules} --trust --defaults
4. Write TASK.md with developer instructions
5. Implement business logic according to specification
6. Commit all changes with descriptive message
7. Push to repository

## Expected Output
After completing, report the commit SHA.
"""
```

#### 2.2 Упростить engineering.py

**Файл**: `services/langgraph/src/subgraphs/engineering.py`

```python
def create_engineering_subgraph() -> StateGraph:
    """Simplified engineering subgraph: Developer → Tester → END"""
    
    graph = StateGraph(EngineeringState)
    
    # Only two nodes
    graph.add_node("developer", developer_node.run)
    graph.add_node("tester", tester_node.run)
    graph.add_node("done", done_node.run)
    graph.add_node("blocked", blocked_node.run)
    
    # Simple flow
    graph.add_edge(START, "developer")
    graph.add_edge("developer", "tester")
    
    graph.add_conditional_edges(
        "tester",
        route_after_tester,
        {"done": "done", "blocked": "blocked", "developer": "developer"},
    )
    
    graph.add_edge("done", END)
    graph.add_edge("blocked", END)
    
    return graph.compile()
```

#### 2.3 Удалить неиспользуемые файлы

- `services/langgraph/src/nodes/architect.py` — удалить или deprecated
- `services/langgraph/src/nodes/preparer.py` — удалить
- `services/langgraph/src/clients/preparer_spawner.py` — удалить
- `services/langgraph/src/templates/` — переместить в developer.py или удалить

---

### Phase 3: Удалить Preparer сервис

**Цель**: убрать preparer из кодовой базы

#### 3.1 Удалить директорию

```bash
rm -rf services/preparer/
```

#### 3.2 Удалить из docker-compose.yml

Если preparer был в docker-compose — удалить.

#### 3.3 Удалить из Makefile

Убрать `build-preparer` target.

---

### Phase 4: Тестирование

#### 4.1 Unit тесты

```bash
make test-workers-spawner  # Проверить что COPIER capability работает
make test-langgraph        # Проверить новый simplified subgraph
```

#### 4.2 Integration тест

1. Отправить в Telegram: "Создай бота который отвечает hello world"
2. Проверить логи:
   - engineering-worker получил job
   - Developer node создал config с copier
   - workers-spawner установил copier
   - Claude выполнил scaffolding + код
   - Commit появился в GitHub repo

#### 4.3 Manual verification

```bash
# Смотреть логи
docker compose logs -f engineering-worker workers-spawner

# Проверить GitHub repo
# Должен быть коммит с service-template структурой + бизнес-логикой
```

---

## EngineeringState после изменений

```python
class EngineeringState(TypedDict):
    messages: Annotated[list, add_messages]
    
    # Project info
    current_project: str | None
    project_spec: dict | None
    allocated_resources: dict
    
    # Result
    engineering_status: str  # "idle" | "working" | "done" | "blocked"
    commit_sha: str | None
    
    # Loop tracking
    iteration_count: int
    test_results: dict | None
    
    # Human-in-the-loop
    needs_human_approval: bool
    human_approval_reason: str | None
    
    # Errors
    errors: Annotated[list[str], _merge_errors]
```

Убраны:
- `repo_info` — Claude сам создаёт repo
- `project_complexity` — не нужно для routing
- `architect_complete` — нет architect node
- `selected_modules` — передаётся в task message
- `deployment_hints` — передаётся в task message
- `custom_task_instructions` — в task message
- `repo_prepared` — нет preparer
- `preparer_commit_sha` → `commit_sha`
- `review_feedback` — пока не используется

---

## Риски и митигации

| Риск | Митигация |
|------|-----------|
| Claude может не справиться с copier | Добавить в промпт примеры команд |
| Потеря granular control | В будущем вернуть отдельные nodes если нужно |
| Увеличение времени на одну задачу | Мониторить через логи, оптимизировать промпт |

---

## Критерии успеха

1. ✅ `make test-workers-spawner` проходит
2. ✅ `make test-langgraph` проходит
3. ✅ E2E: Telegram → GitHub repo с кодом
4. ✅ Нет упоминаний preparer в активном коде
5. ✅ Engineering subgraph: только Developer + Tester nodes

---

## Порядок выполнения

1. [x] Phase 1: COPIER capability (30 min) ✅
2. [x] Phase 2: Simplified subgraph (1-2 hours) ✅
3. [x] Phase 3: Remove preparer (10 min) ✅
4. [x] Phase 4: Testing (30 min) ✅

**Результат**: Все фазы завершены и проверены. Engineering subgraph упрощен с 9 до 4 нод.

