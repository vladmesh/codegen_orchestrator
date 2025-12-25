"""Developer agent node.

Orchestrates the implementation of business logic by spawning a Factory.ai worker.
This node runs after the Architect has set up the initial project structure.
"""

import logging
import os

from langchain_core.messages import AIMessage

from ..clients.github import GitHubAppClient
from ..clients.worker_spawner import request_spawn

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Developer, the lead engineer agent in the codegen orchestrator.

Your job:
1. Review the project structure created by the Architect.
2. Implement the business logic defined in the creation domains and potential specifications.
3. Ensure all tests pass.

## Available tools:
- None for now (Coordinator spawns the worker directly)

## Workflow:
1. Receive the repository info and project spec.
2. Spawn a coding worker to implement the logic.
3. Report the result.

## Guidelines:
- Follow the service-template patterns.
- **READ `AGENTS.md`** in the repository for instructions.
- Use `make generate-from-spec` to generate code from specifications.
- Ensure 100% test coverage for new logic.
- Use best practices for Python/FastAPI development.
"""


async def run(state: dict) -> dict:
    """Run developer agent.

    Currently just a localized step to prepare for worker spawning.
    In the future, this could analyze the codebase before spawning.
    """
    return {
        "current_agent": "developer",
    }


async def spawn_developer_worker(state: dict) -> dict:
    """Spawn Factory.ai worker to implement business logic."""
    repo_info = state.get("repo_info", {})
    project_spec = state.get("project_spec", {})

    if not repo_info:
        return {
            "messages": [
                AIMessage(content="❌ No repository info found. Cannot spawn developer worker.")
            ],
            "errors": state.get("errors", []) + ["No repository info for developer worker"],
        }

    repo_full_name = repo_info.get("full_name")
    if not repo_full_name:
        return {
            "messages": [AIMessage(content="❌ Repository full_name not found.")],
            "errors": state.get("errors", []) + ["Repository full_name missing"],
        }

    # Get fresh token for the repo
    github_client = GitHubAppClient()
    owner, repo = repo_full_name.split("/")

    try:
        token = await github_client.get_token(owner, repo)
    except Exception as e:
        logger.exception(f"Failed to get GitHub token: {e}")
        return {
            "messages": [AIMessage(content=f"❌ Failed to get GitHub token: {e}")],
            "errors": state.get("errors", []) + [str(e)],
        }

    # Build task for Factory.ai
    task_content = f"""# Project: {project_spec.get("name", "project")}

## Context
The Architect agent has initialized the project using `service-template`.
Repository: {repo_full_name}

## Task
Implement the business logic for the project based on the domain specifications.

1.  **Understand the Plan**:
    - Read `AGENTS.md` and `README.md` in the repository root.
    - Review `shared/spec/` (or `domains/`) for Open API/Async API specs.

2.  **Generate Code**:
    - Run `make generate-from-spec` (or equivalent in Makefile) to generate
      API routers and models from specs.
    - Validate generated code with `make lint`.

3.  **Implement Logic**:
    - Implement domain services and repositories.
    - Hook up the generated API routers to the services (Controllers/Wiring).
    - Ensure Clean Architecture is followed.

4.  **Verification**:
    - Ensure `make test` passes.
    - Ensure `make lint` passes.

## Requirements
- Use the existing project structure.
- Follow Clean Architecture.
- Add unit and integration tests.

## Commit Message
Implement business logic for {project_spec.get("name", "project")}
"""

    logger.info(f"Spawning Developer worker for {repo_full_name}")

    result = await request_spawn(
        repo=repo_full_name,
        github_token=token,
        task_content=task_content,
        task_title=f"Implement business logic for {project_spec.get('name', 'project')}",
        model=os.getenv("FACTORY_MODEL", "claude-sonnet-4-5-20250929"),
    )

    if result.success:
        message = f"""✅ Business logic implementation completed!

Repository: {repo_info.get("html_url")}
Commit: {result.commit_sha or "N/A"}

The developer worker has:
- Implemented domain logic
- Created API endpoints
- Verified tests

Project is ready for review/deployment.
"""
        return {
            "messages": [AIMessage(content=message)],
            # "developer_complete": True, # TODO: Add to state if needed
        }
    else:
        return {
            "messages": [
                AIMessage(content=f"❌ Developer worker failed:\n\n{result.output[-500:]}")
            ],
            "errors": state.get("errors", []) + ["Developer worker failed"],
        }
