# Architect Refactor Plan: Simplified LLM + Preparer Container

> **Status**: Planning
> **Created**: 2024-12-28
> **Goal**: Упростить Architect до чистой LLM ноды, вынести подготовку проекта в lightweight Preparer Container, сделать Developer максимально автономным.

## Table of Contents

1. [Контекст проблемы](#контекст-проблемы)
2. [Текущая архитектура](#текущая-архитектура)
3. [Новая архитектура](#новая-архитектура)
4. [Детали реализации](#детали-реализации)
5. [Итеративный план](#итеративный-план)
6. [Изменения в сидировании](#изменения-в-сидировании)
7. [Юнит тесты](#юнит-тесты)
8. [Риски и митигация](#риски-и-митигация)

---

## Контекст проблемы

### Проблема с текущим Architect

1. **Двухстадийность**: Architect = LLM нода + Factory.ai Worker
   - LLM создает репо, выбирает сложность
   - Factory.ai Worker выполняет copier, пишет начальные спеки
   - Это два отдельных LLM вызова, дорого по токенам

2. **Factory.ai для простой задачи**: Copier + начальная структура — детерминированная задача, не требует LLM
   - Factory.ai жрёт токены на то что можно сделать bash-скриптом
   - Timeout 10 минут на задачу которая занимает 30 секунд

3. **Разделение ответственности размыто**:
   - Architect Worker пишет начальные спеки
   - Developer потом их дополняет/переписывает
   - Если Architect написал невалидные спеки — Developer тратит итерации на фикс

### Целевое состояние

1. **Architect = простая LLM нода**:
   - Выбирает модули из service-template
   - Генерирует TASK.md для Developer
   - Определяет deployment hints для DevOps
   - НЕ пишет спеки, НЕ работает с кодом

2. **Preparer Container = детерминированная подготовка**:
   - Легковесный контейнер (без Factory.ai)
   - Выполняет copier с выбранными модулями
   - Кладёт TASK.md, AGENTS.md
   - Git push готового проекта
   - Timeout 60 секунд

3. **Developer = максимально автономен**:
   - Получает готовый проект с модулями
   - Сам пишет спеки (models.yaml, domains)
   - Сам вызывает `make generate-from-spec`
   - Сам реализует контроллеры
   - Factory.ai/Claude Code делают сложную работу

---

## Текущая архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                    Engineering Subgraph                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌─────────────────────┐                │
│  │ Architect    │───▶│ Architect Worker    │                │
│  │ (LLM Node)   │    │ (Factory.ai)        │                │
│  │              │    │                     │                │
│  │ • create_repo│    │ • copier copy       │                │
│  │ • set_complex│    │ • write specs       │                │
│  │ • get_token  │    │ • git push          │                │
│  └──────────────┘    └─────────────────────┘                │
│         │                     │                              │
│         │                     ▼                              │
│         │           ┌─────────────────────┐                 │
│         │           │ Developer Worker    │                 │
│         └──────────▶│ (Factory.ai)        │                 │
│                     │                     │                 │
│                     │ • implement logic   │                 │
│                     │ • run tests         │                 │
│                     └─────────────────────┘                 │
│                              │                               │
│                              ▼                               │
│                     ┌───────────────┐                       │
│                     │    Tester     │                       │
│                     └───────────────┘                       │
└─────────────────────────────────────────────────────────────┘
```

### Текущие файлы

| Файл | Описание |
|------|----------|
| `services/langgraph/src/nodes/architect.py` | ArchitectNode (LLM) + ArchitectWorkerNode (Factory) |
| `services/langgraph/src/subgraphs/engineering.py` | Engineering subgraph с routing |
| `services/langgraph/src/clients/worker_spawner.py` | Клиент для spawn контейнеров |
| `scripts/agent_configs.yaml` | Промпты для LLM нод |
| `scripts/cli_agent_configs.yaml` | Промпты для CLI workers |

---

## Новая архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                    Engineering Subgraph                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────────────────┐               │
│  │           Architect (LLM Node)            │               │
│  │                                           │               │
│  │  Tools:                                   │               │
│  │  • create_github_repo(name, desc)         │               │
│  │  • select_modules(["backend", "tg_bot"])  │               │
│  │  • set_deployment_hints({domain, ports})  │               │
│  │  • customize_task_instructions(text)      │               │
│  │                                           │               │
│  │  LLM decides:                             │               │
│  │  • Which modules based on project spec    │               │
│  │  • Custom instructions for developer      │               │
│  │  • Deployment requirements                │               │
│  └──────────────────────────────────────────┘               │
│                        │                                     │
│                        ▼                                     │
│  ┌──────────────────────────────────────────┐               │
│  │      Preparer (Python Functional Node)    │               │
│  │                                           │               │
│  │  • Spawns lightweight container           │               │
│  │  • copier copy with selected modules      │               │
│  │  • Writes TASK.md, AGENTS.md              │               │
│  │  • Git push                               │               │
│  │  • Container exits (no Factory.ai)        │               │
│  │                                           │               │
│  │  Timeout: 60 seconds                      │               │
│  └──────────────────────────────────────────┘               │
│                        │                                     │
│                        ▼                                     │
│  ┌──────────────────────────────────────────┐               │
│  │       Developer Worker (Factory.ai)       │               │
│  │                                           │               │
│  │  Receives:                                │               │
│  │  • Ready project with modules             │               │
│  │  • TASK.md with instructions              │               │
│  │  • AGENTS.md with framework guide         │               │
│  │                                           │               │
│  │  Does:                                    │               │
│  │  • Write specs (models.yaml, domains)     │               │
│  │  • make generate-from-spec                │               │
│  │  • Implement controllers                  │               │
│  │  • Run tests                              │               │
│  │  • Git push                               │               │
│  └──────────────────────────────────────────┘               │
│                        │                                     │
│                        ▼                                     │
│                 ┌───────────────┐                           │
│                 │    Tester     │                           │
│                 └───────────────┘                           │
└─────────────────────────────────────────────────────────────┘
```

### Новый State

```python
class EngineeringState(TypedDict):
    # Existing
    messages: Annotated[list, add_messages]
    current_project: str | None
    project_spec: dict | None
    repo_info: dict | None

    # New: Architect outputs
    selected_modules: list[str]           # ["backend", "tg_bot"]
    deployment_hints: dict | None         # {"domain": "...", "ports": {...}}
    custom_task_instructions: str | None  # Extra instructions for developer

    # New: Preparer outputs
    repo_prepared: bool                   # True after preparer completes
    preparer_commit_sha: str | None       # Commit SHA from preparer

    # Existing developer/tester fields...
```

---

## Детали реализации

### 1. Architect Tools

```python
# services/langgraph/src/tools/architect_tools.py

AVAILABLE_MODULES = ["backend", "tg_bot", "notifications", "frontend"]

@tool
def select_modules(modules: list[str]) -> str:
    """
    Select which modules to include in the project.

    Available modules:
    - backend: FastAPI REST API with PostgreSQL database
    - tg_bot: Telegram bot message handler (requires telegram token)
    - notifications: Background notifications processor (email, telegram)
    - frontend: Node.js frontend (port 4321)

    Choose based on project requirements. Most projects need 'backend'.
    If project involves Telegram bot, include 'tg_bot'.
    """
    invalid = [m for m in modules if m not in AVAILABLE_MODULES]
    if invalid:
        return f"Error: Invalid modules: {invalid}. Available: {AVAILABLE_MODULES}"
    return f"Selected modules: {modules}"

@tool
def set_deployment_hints(
    domain: str | None = None,
    backend_port: int = 8000,
    frontend_port: int = 4321,
    needs_ssl: bool = True,
    environment_vars: list[str] | None = None
) -> str:
    """
    Set deployment configuration hints for DevOps.

    Args:
        domain: Custom domain if needed (e.g., "myapp.example.com")
        backend_port: Port for backend service (default 8000)
        frontend_port: Port for frontend if included (default 4321)
        needs_ssl: Whether to configure SSL (default True)
        environment_vars: List of required env vars (e.g., ["TELEGRAM_TOKEN", "OPENAI_KEY"])
    """
    return "Deployment hints saved"

@tool
def customize_task_instructions(instructions: str) -> str:
    """
    Add custom instructions for the developer.

    Use this to provide project-specific context that isn't in the spec.
    For example:
    - Special API integrations needed
    - Specific business logic requirements
    - Performance considerations
    """
    return "Custom instructions saved"
```

### 2. Preparer Container

```dockerfile
# services/preparer/Dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir copier gitpython

COPY prepare.py /prepare.py

ENTRYPOINT ["python", "/prepare.py"]
```

```python
# services/preparer/prepare.py
import os
import subprocess
import sys
from pathlib import Path

def main():
    # Read environment
    repo_url = os.environ["REPO_URL"]
    modules = os.environ["MODULES"]  # comma-separated: "backend,tg_bot"
    project_name = os.environ["PROJECT_NAME"]
    task_md = os.environ.get("TASK_MD", "")
    agents_md = os.environ.get("AGENTS_MD", "")
    github_token = os.environ["GITHUB_TOKEN"]

    # Setup git auth
    auth_url = repo_url.replace("https://", f"https://x-access-token:{github_token}@")

    workspace = Path("/workspace")
    workspace.mkdir(exist_ok=True)
    os.chdir(workspace)

    # Clone empty repo
    subprocess.run(["git", "clone", auth_url, "."], check=True)

    # Run copier
    subprocess.run([
        "copier", "copy", "gh:vladmesh/service-template", ".",
        "--data", f"project_name={project_name}",
        "--data", f"modules={modules}",
        "--trust", "--defaults", "--overwrite"
    ], check=True)

    # Write TASK.md and AGENTS.md
    if task_md:
        Path("TASK.md").write_text(task_md)
    if agents_md:
        Path("AGENTS.md").write_text(agents_md)

    # Git commit and push
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run([
        "git", "commit", "-m", "Initial project structure via service-template"
    ], check=True)
    subprocess.run(["git", "push"], check=True)

    # Output commit SHA for state
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True
    )
    print(f"COMMIT_SHA={result.stdout.strip()}")

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
```

### 3. Preparer Node

```python
# services/langgraph/src/nodes/preparer.py

from nodes.base import FunctionalNode
from clients.preparer_spawner import spawn_preparer

class PreparerNode(FunctionalNode):
    """Spawns lightweight container to prepare project structure."""

    async def run(self, state: dict) -> dict:
        repo_info = state["repo_info"]
        selected_modules = state.get("selected_modules", ["backend"])
        project_spec = state.get("project_spec", {})
        custom_instructions = state.get("custom_task_instructions", "")

        # Render TASK.md
        task_md = self._render_task_md(
            project_spec=project_spec,
            modules=selected_modules,
            custom_instructions=custom_instructions
        )

        # Render AGENTS.md (static template + module-specific sections)
        agents_md = self._render_agents_md(modules=selected_modules)

        # Spawn preparer container
        result = await spawn_preparer(
            repo_url=repo_info["clone_url"],
            project_name=repo_info["name"],
            modules=",".join(selected_modules),
            task_md=task_md,
            agents_md=agents_md,
            github_token=await self._get_github_token(repo_info["full_name"])
        )

        if not result.success:
            return {
                "errors": [f"Preparer failed: {result.error}"],
                "repo_prepared": False
            }

        return {
            "repo_prepared": True,
            "preparer_commit_sha": result.commit_sha,
            "current_agent": "preparer_complete"
        }

    def _render_task_md(self, project_spec: dict, modules: list, custom_instructions: str) -> str:
        """Render TASK.md template with project context."""
        # Template loaded from file or hardcoded
        return TASK_MD_TEMPLATE.format(
            project_name=project_spec.get("name", "project"),
            description=project_spec.get("description", ""),
            detailed_spec=project_spec.get("detailed_spec", ""),
            modules=", ".join(modules),
            custom_instructions=custom_instructions or "No additional instructions."
        )

    def _render_agents_md(self, modules: list) -> str:
        """Render AGENTS.md with module-specific guidance."""
        # Base template + conditional sections per module
        return AGENTS_MD_TEMPLATE  # From service-template
```

### 4. TASK.md Template

```markdown
# Task: Implement {project_name}

## Project Description
{description}

## Detailed Specification
{detailed_spec}

## Selected Modules
{modules}

## Your Task

You are the Developer agent. The project structure has been prepared with the following modules: {modules}.

### Step 1: Understand the Structure
- Read `AGENTS.md` for framework conventions
- Explore `services/` directory to see available modules
- Check `shared/spec/` for spec file locations

### Step 2: Define Specifications
Based on the project requirements, create YAML specifications:

1. **Models** (`shared/spec/models.yaml`):
   - Define all data models with their fields
   - Include variants (Create, Update, Read)
   - Example structure provided in file

2. **Domain Specs** (`services/<module>/spec/<domain>.yaml`):
   - Define operations (CRUD, custom actions)
   - Map to REST endpoints
   - Configure events if needed

### Step 3: Generate Code
Run: `make generate-from-spec`

This will generate:
- Pydantic schemas in `shared/shared/generated/`
- FastAPI routers in `services/*/src/generated/routers/`
- Protocol interfaces in `services/*/src/generated/protocols.py`

### Step 4: Implement Controllers
- Implement business logic in `services/*/src/controllers/`
- Follow the Protocol interface from generated code
- Use async patterns (Python 3.12+)

### Step 5: Test
Run: `make test`

Ensure all tests pass before committing.

### Step 6: Commit and Push
```bash
git add .
git commit -m "Implement {project_name} business logic"
git push
```

## Custom Instructions
{custom_instructions}

## Important Notes
- Do NOT modify files in `src/generated/` - they are auto-generated
- Follow the patterns in `AGENTS.md`
- Use structlog for logging
- All code must be async-ready
```

---

## Итеративный план

### Phase 1: Подготовка инфраструктуры (Preparer Container)

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 1.1 | Создать Dockerfile для preparer | `services/preparer/Dockerfile` | - |
| 1.2 | Написать prepare.py скрипт | `services/preparer/prepare.py` | Unit test: mock subprocess |
| 1.3 | Добавить preparer в docker-compose | `docker-compose.yml` | - |
| 1.4 | Создать клиент spawn_preparer | `services/langgraph/src/clients/preparer_spawner.py` | Unit test: mock Redis |
| 1.5 | Добавить preparer image в Makefile | `Makefile` | - |

### Phase 2: Новые Architect Tools

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 2.1 | Создать select_modules tool | `services/langgraph/src/tools/architect_tools.py` | Unit test: validation |
| 2.2 | Создать set_deployment_hints tool | `services/langgraph/src/tools/architect_tools.py` | Unit test |
| 2.3 | Создать customize_task_instructions tool | `services/langgraph/src/tools/architect_tools.py` | Unit test |
| 2.4 | Обновить handle_tool_result в ArchitectNode | `services/langgraph/src/nodes/architect.py` | Unit test: state updates |

### Phase 3: Preparer Node

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 3.1 | Создать TASK.md шаблон | `services/langgraph/src/templates/task_md.py` | Unit test: rendering |
| 3.2 | Создать AGENTS.md шаблон | `services/langgraph/src/templates/agents_md.py` | Unit test: rendering |
| 3.3 | Реализовать PreparerNode | `services/langgraph/src/nodes/preparer.py` | Unit test: mock spawner |
| 3.4 | Добавить PreparerNode в exports | `services/langgraph/src/nodes/__init__.py` | - |

### Phase 4: Обновление Engineering Subgraph

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 4.1 | Добавить новые поля в EngineeringState | `services/langgraph/src/subgraphs/engineering.py` | - |
| 4.2 | Удалить ArchitectWorkerNode из графа | `services/langgraph/src/subgraphs/engineering.py` | - |
| 4.3 | Добавить PreparerNode после Architect | `services/langgraph/src/subgraphs/engineering.py` | - |
| 4.4 | Обновить routing logic | `services/langgraph/src/subgraphs/engineering.py` | Integration test |
| 4.5 | Удалить complexity routing (теперь всегда Developer) | `services/langgraph/src/subgraphs/engineering.py` | - |

### Phase 5: Обновление сидирования (Prompts)

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 5.1 | Обновить промпт Architect | `scripts/agent_configs.yaml` | Manual test |
| 5.2 | Удалить architect.spawn_factory_worker | `scripts/cli_agent_configs.yaml` | - |
| 5.3 | Обновить промпт Developer (ссылка на TASK.md) | `scripts/agent_configs.yaml` | Manual test |
| 5.4 | Обновить developer CLI prompt | `scripts/cli_agent_configs.yaml` | Manual test |
| 5.5 | Запустить make seed для применения | - | - |

### Phase 6: Cleanup

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 6.1 | Удалить ArchitectWorkerNode класс | `services/langgraph/src/nodes/architect.py` | - |
| 6.2 | Удалить FactoryNode base class если не используется | `services/langgraph/src/nodes/base.py` | - |
| 6.3 | Обновить документацию NODES.md | `docs/NODES.md` | - |
| 6.4 | Обновить ARCHITECTURE.md | `ARCHITECTURE.md` | - |

### Phase 7: Integration Testing

| # | Задача | Файлы | Тесты |
|---|--------|-------|-------|
| 7.1 | E2E тест: Architect → Preparer → Developer | `services/langgraph/tests/integration/` | Integration |
| 7.2 | Тест с реальным service-template | Manual | Manual |
| 7.3 | Тест failure scenarios | `services/langgraph/tests/integration/` | Integration |

---

## Изменения в сидировании

### Новый промпт Architect (`scripts/agent_configs.yaml`)

```yaml
- id: "architect"
  name: "Architect"
  llm_provider: "openrouter"
  model_identifier: "openai/gpt-4o"
  model_name: "GPT-4o via OpenRouter"
  temperature: 0.0
  openrouter_app_name: "Codegen Orchestrator"
  system_prompt: |
    You are Architect, the project planning agent in the codegen orchestrator.

    Your job:
    1. Analyze project requirements and select appropriate modules.
    2. Create a GitHub repository for the project.
    3. Configure deployment hints for DevOps.
    4. Optionally add custom instructions for the Developer.

    ## Available Tools

    ### select_modules(modules: list[str])
    Choose which modules to include. Available:
    - **backend**: FastAPI REST API with PostgreSQL (most projects need this)
    - **tg_bot**: Telegram bot handler (if project involves Telegram)
    - **notifications**: Background notification processor
    - **frontend**: Node.js frontend

    ### create_github_repo(name: str, description: str)
    Create a new private GitHub repository.

    ### set_deployment_hints(domain, backend_port, frontend_port, needs_ssl, environment_vars)
    Configure deployment. Use defaults unless specific requirements exist.

    ### customize_task_instructions(instructions: str)
    Add project-specific instructions for the Developer. Use when:
    - Special API integrations are needed
    - Specific business logic patterns required
    - External services must be integrated

    ## Workflow

    1. Analyze the project spec to understand requirements
    2. Call `select_modules` with appropriate modules
    3. Call `create_github_repo` with snake_case name
    4. Optionally call `set_deployment_hints` if custom config needed
    5. Optionally call `customize_task_instructions` for special requirements

    ## Module Selection Guide

    | Project Type | Recommended Modules |
    |--------------|---------------------|
    | Telegram bot | backend, tg_bot |
    | REST API only | backend |
    | Full-stack web app | backend, frontend |
    | Bot with notifications | backend, tg_bot, notifications |

    ## CRITICAL INSTRUCTIONS
    - You MUST call `select_modules` first to choose modules
    - You MUST call `create_github_repo` to create the repository
    - Do NOT implement any code - only plan and configure
    - Do NOT write specifications - Developer will handle that
    - Keep it simple: most projects need just "backend" or "backend, tg_bot"

    ## Current Project Info
    {project_info}

    ## Allocated Resources
    {allocated_resources}
```

### Удалить из `scripts/cli_agent_configs.yaml`

```yaml
# DELETE THIS ENTIRE BLOCK:
- id: "architect.spawn_factory_worker"
  name: "Architect Factory Worker"
  # ... all of this config
```

### Новый промпт Developer (`scripts/agent_configs.yaml`)

```yaml
- id: "developer"
  name: "Developer"
  llm_provider: "openrouter"
  model_identifier: "openai/gpt-4o"
  model_name: "GPT-4o via OpenRouter"
  temperature: 0.0
  openrouter_app_name: "Codegen Orchestrator"
  system_prompt: |
    You are Developer, the implementation agent in the codegen orchestrator.

    Your job:
    1. Read TASK.md for project requirements and instructions.
    2. Write specifications in YAML format.
    3. Generate code from specs using `make generate-from-spec`.
    4. Implement business logic in controllers.
    5. Ensure tests pass.

    ## Important Files

    - `TASK.md` - Your instructions and project requirements
    - `AGENTS.md` - Framework conventions and patterns
    - `shared/spec/models.yaml` - Data model definitions
    - `services/*/spec/*.yaml` - Domain specifications

    ## Your Workflow

    The project structure is already prepared with selected modules.
    Follow the instructions in TASK.md step by step.

    ## Key Commands

    - `make generate-from-spec` - Generate code from YAML specs
    - `make lint` - Check code quality
    - `make test` - Run tests
    - `make dev-start` - Start services locally (for debugging)

    ## Guidelines

    - Read TASK.md first - it contains all the context you need
    - Follow AGENTS.md patterns strictly
    - Do NOT modify files in `src/generated/` directories
    - Use async patterns throughout (Python 3.12+)
    - Use structlog for logging
    - Ensure 100% test coverage for new logic
```

### Обновить Developer CLI prompt (`scripts/cli_agent_configs.yaml`)

```yaml
- id: "developer.spawn_factory_worker"
  name: "Developer Factory Worker"
  provider: "factory"
  model_name: "claude-sonnet-4-20250514"
  timeout_seconds: 900  # 15 minutes - developer does more work now
  required_credentials:
    - "GITHUB_TOKEN"
  provider_settings:
    autonomy: "full"
  prompt_template: |
    # Project: {project_name}

    ## Your Task

    Read `TASK.md` in the repository root - it contains all instructions.

    Follow the steps in TASK.md:
    1. Understand the project structure
    2. Write specifications (models.yaml, domain specs)
    3. Run `make generate-from-spec`
    4. Implement controllers with business logic
    5. Run tests with `make test`
    6. Commit and push your changes

    ## Context from Orchestrator

    ### Project Specification
    {project_spec}

    ### Selected Modules
    {modules}

    ### Deployment Hints
    {deployment_hints}

    ## Critical Instructions

    - Start by reading TASK.md and AGENTS.md
    - Write specs before implementing anything
    - Always run `make generate-from-spec` after changing specs
    - Do NOT edit files in `src/generated/` directories
    - Commit frequently with meaningful messages
    - Push when done: `git push`
```

---

## Юнит тесты

### Test 1: select_modules validation

```python
# services/langgraph/tests/unit/tools/test_architect_tools.py

import pytest
from tools.architect_tools import select_modules, AVAILABLE_MODULES

class TestSelectModules:
    def test_valid_modules(self):
        result = select_modules.invoke({"modules": ["backend", "tg_bot"]})
        assert "Selected modules" in result
        assert "backend" in result
        assert "tg_bot" in result

    def test_invalid_module(self):
        result = select_modules.invoke({"modules": ["backend", "invalid_module"]})
        assert "Error" in result
        assert "invalid_module" in result

    def test_empty_modules(self):
        result = select_modules.invoke({"modules": []})
        assert "Selected modules: []" in result

    def test_all_valid_modules(self):
        result = select_modules.invoke({"modules": AVAILABLE_MODULES})
        assert "Selected modules" in result
        for module in AVAILABLE_MODULES:
            assert module in result
```

### Test 2: PreparerNode state updates

```python
# services/langgraph/tests/unit/nodes/test_preparer.py

import pytest
from unittest.mock import AsyncMock, patch
from nodes.preparer import PreparerNode

class TestPreparerNode:
    @pytest.fixture
    def preparer_node(self):
        return PreparerNode()

    @pytest.fixture
    def mock_state(self):
        return {
            "repo_info": {
                "name": "test_project",
                "full_name": "user/test_project",
                "clone_url": "https://github.com/user/test_project.git"
            },
            "selected_modules": ["backend", "tg_bot"],
            "project_spec": {
                "name": "test_project",
                "description": "A test project",
                "detailed_spec": "Build a bot that..."
            },
            "custom_task_instructions": "Use Redis for caching"
        }

    @pytest.mark.asyncio
    async def test_successful_preparation(self, preparer_node, mock_state):
        with patch.object(preparer_node, '_get_github_token', return_value="token"):
            with patch('nodes.preparer.spawn_preparer') as mock_spawn:
                mock_spawn.return_value = AsyncMock(
                    success=True,
                    commit_sha="abc123"
                )()

                result = await preparer_node.run(mock_state)

                assert result["repo_prepared"] is True
                assert result["preparer_commit_sha"] == "abc123"
                assert "errors" not in result

    @pytest.mark.asyncio
    async def test_failed_preparation(self, preparer_node, mock_state):
        with patch.object(preparer_node, '_get_github_token', return_value="token"):
            with patch('nodes.preparer.spawn_preparer') as mock_spawn:
                mock_spawn.return_value = AsyncMock(
                    success=False,
                    error="copier failed"
                )()

                result = await preparer_node.run(mock_state)

                assert result["repo_prepared"] is False
                assert "copier failed" in result["errors"][0]

    def test_render_task_md(self, preparer_node):
        task_md = preparer_node._render_task_md(
            project_spec={"name": "test", "description": "A test"},
            modules=["backend"],
            custom_instructions="Special note"
        )

        assert "test" in task_md
        assert "backend" in task_md
        assert "Special note" in task_md

    def test_render_task_md_no_custom_instructions(self, preparer_node):
        task_md = preparer_node._render_task_md(
            project_spec={"name": "test", "description": "A test"},
            modules=["backend"],
            custom_instructions=""
        )

        assert "No additional instructions" in task_md
```

### Test 3: Architect handle_tool_result

```python
# services/langgraph/tests/unit/nodes/test_architect.py

import pytest
from nodes.architect import ArchitectNode

class TestArchitectNodeToolResults:
    @pytest.fixture
    def architect_node(self):
        return ArchitectNode()

    def test_handle_select_modules(self, architect_node):
        state = {}
        tool_call = {"name": "select_modules", "args": {"modules": ["backend", "tg_bot"]}}
        tool_result = "Selected modules: ['backend', 'tg_bot']"

        updates = architect_node.handle_tool_result(state, tool_call, tool_result)

        assert updates["selected_modules"] == ["backend", "tg_bot"]

    def test_handle_set_deployment_hints(self, architect_node):
        state = {}
        tool_call = {
            "name": "set_deployment_hints",
            "args": {
                "domain": "myapp.com",
                "backend_port": 8080,
                "needs_ssl": True
            }
        }
        tool_result = "Deployment hints saved"

        updates = architect_node.handle_tool_result(state, tool_call, tool_result)

        assert updates["deployment_hints"]["domain"] == "myapp.com"
        assert updates["deployment_hints"]["backend_port"] == 8080
        assert updates["deployment_hints"]["needs_ssl"] is True

    def test_handle_customize_task_instructions(self, architect_node):
        state = {}
        tool_call = {
            "name": "customize_task_instructions",
            "args": {"instructions": "Use Redis for caching"}
        }
        tool_result = "Custom instructions saved"

        updates = architect_node.handle_tool_result(state, tool_call, tool_result)

        assert updates["custom_task_instructions"] == "Use Redis for caching"

    def test_handle_create_github_repo(self, architect_node):
        state = {}
        tool_call = {
            "name": "create_github_repo",
            "args": {"name": "my_project", "description": "A project"}
        }
        tool_result = '{"name": "my_project", "full_name": "user/my_project", "clone_url": "..."}'

        updates = architect_node.handle_tool_result(state, tool_call, tool_result)

        assert "repo_info" in updates
```

### Test 4: preparer_spawner client

```python
# services/langgraph/tests/unit/clients/test_preparer_spawner.py

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from clients.preparer_spawner import spawn_preparer, PreparerResult

class TestPreparerSpawner:
    @pytest.mark.asyncio
    async def test_spawn_preparer_success(self):
        mock_redis = AsyncMock()
        mock_redis.publish = AsyncMock()
        mock_redis.subscribe = AsyncMock()

        # Mock the pubsub message
        mock_message = {
            "type": "message",
            "data": b'{"success": true, "commit_sha": "abc123"}'
        }
        mock_redis.pubsub.return_value.get_message = AsyncMock(return_value=mock_message)

        with patch('clients.preparer_spawner.get_redis', return_value=mock_redis):
            result = await spawn_preparer(
                repo_url="https://github.com/user/repo.git",
                project_name="test_project",
                modules="backend,tg_bot",
                task_md="# Task",
                agents_md="# Agents",
                github_token="token123"
            )

            assert isinstance(result, PreparerResult)
            # Further assertions based on implementation

    @pytest.mark.asyncio
    async def test_spawn_preparer_timeout(self):
        mock_redis = AsyncMock()
        mock_redis.pubsub.return_value.get_message = AsyncMock(return_value=None)

        with patch('clients.preparer_spawner.get_redis', return_value=mock_redis):
            with pytest.raises(TimeoutError):
                await spawn_preparer(
                    repo_url="https://github.com/user/repo.git",
                    project_name="test_project",
                    modules="backend",
                    task_md="# Task",
                    agents_md="# Agents",
                    github_token="token123",
                    timeout_seconds=1
                )
```

---

## Риски и митигация

| Риск | Вероятность | Импакт | Митигация |
|------|-------------|--------|-----------|
| Copier в preparer падает | Medium | High | Retry logic, детальные логи, fallback на ручное создание |
| Developer не справляется с полным циклом | Medium | High | Увеличить max_iterations до 5, улучшить TASK.md |
| service-template несовместим с copier версией | Low | High | Зафиксировать версию copier в Dockerfile |
| GitHub rate limits при clone | Low | Medium | Кэширование токенов, exponential backoff |
| TASK.md слишком большой для context | Low | Medium | Сжать промпт, ссылаться на файлы вместо inline |

---

## Критерии успеха

1. **Architect выполняется за < 30 секунд** (сейчас ~5-10 минут с Factory worker)
2. **Preparer выполняется за < 60 секунд**
3. **Developer получает готовую структуру** и не тратит время на copier
4. **Снижение стоимости токенов** за счет удаления Factory.ai из Architect
5. **E2E тест проходит**: Telegram bot проект от спеки до готового кода

---

## Appendix: Ключевые файлы для изменения

```
services/
├── langgraph/
│   └── src/
│       ├── nodes/
│       │   ├── architect.py      # MODIFY: remove ArchitectWorkerNode, add new tools
│       │   ├── preparer.py       # NEW: PreparerNode
│       │   └── base.py           # MAYBE: cleanup unused classes
│       ├── tools/
│       │   └── architect_tools.py # NEW: select_modules, set_deployment_hints
│       ├── clients/
│       │   └── preparer_spawner.py # NEW: spawn_preparer client
│       ├── templates/
│       │   ├── task_md.py        # NEW: TASK.md template
│       │   └── agents_md.py      # NEW: AGENTS.md template
│       └── subgraphs/
│           └── engineering.py    # MODIFY: new flow
├── preparer/                      # NEW: entire service
│   ├── Dockerfile
│   └── prepare.py
└── worker-spawner/
    └── src/
        └── spawner.py            # MODIFY: add preparer container support

scripts/
├── agent_configs.yaml            # MODIFY: architect + developer prompts
└── cli_agent_configs.yaml        # MODIFY: remove architect worker, update developer

docs/
├── NODES.md                      # UPDATE: new architecture
└── ARCHITECT_REFACTOR_PLAN.md    # THIS FILE
```
