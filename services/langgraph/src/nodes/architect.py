"""Architect agent node.

Creates project structure using Factory.ai Droid in an isolated container.
Generates high-level architecture without business logic.

Uses BaseAgentNode for dynamic prompt loading from database.
"""

import logging
import os
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool

from ..clients.github import GitHubAppClient
from ..clients.worker_spawner import request_spawn
from ..tools.github import create_github_repo, get_github_token
from .base import BaseAgentNode

logger = logging.getLogger(__name__)


@tool
def set_project_complexity(complexity: str):
    """Set the project complexity level.

    Args:
        complexity: "simple" or "complex"
    """
    return complexity


# Tools available to architect
tools = [create_github_repo, get_github_token, set_project_complexity]


class ArchitectNode(BaseAgentNode):
    """Architect agent that creates project structure."""

    def handle_tool_result(self, tool_name: str, result: Any, state: dict) -> dict[str, Any]:
        """Handle repo creation and complexity setting."""
        updates = {}

        if tool_name == "create_github_repo" and result:
            updates["repo_info"] = result.model_dump() if hasattr(result, "model_dump") else result

        if tool_name == "set_project_complexity" and result:
            updates["project_complexity"] = result

        return updates


# Create singleton instance
_node = ArchitectNode("architect", tools)


async def run(state: dict) -> dict:
    """Run architect agent.

    Creates GitHub repo and prepares for code generation.
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec", {})
    allocated_resources = state.get("allocated_resources", {})

    # Build context for LLM
    project_info = f"""
Name: {project_spec.get("name", "unknown")}
Description: {project_spec.get("description", "No description")}
Modules: {project_spec.get("modules", [])}
Entry Points: {project_spec.get("entry_points", [])}
"""

    # Get dynamic prompt from database
    system_prompt_template = await _node.get_system_prompt()

    # Replace placeholders in prompt
    system_content = system_prompt_template.format(
        project_info=project_info,
        allocated_resources=allocated_resources,
    )

    llm_messages = [SystemMessage(content=system_content)]
    llm_messages.extend(messages)

    # Get configured LLM
    llm_with_tools = await _node.get_llm_with_tools()

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "current_agent": "architect",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Architect LLM.

    Delegates to BaseAgentNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result (repo_info, project_complexity)
    """
    return await _node.execute_tools(state)


async def spawn_factory_worker(state: dict) -> dict:
    """Spawn Factory.ai worker to generate project structure.

    This node is called after repo is created and token is obtained.
    It spawns a Sysbox container with Factory.ai Droid to generate code.
    """
    repo_info = state.get("repo_info", {})
    project_spec = state.get("project_spec", {})
    project_complexity = state.get("project_complexity", "complex")  # Default to complex

    if not repo_info:
        return {
            "messages": [AIMessage(content="❌ No repository info found. Cannot spawn worker.")],
            "errors": state.get("errors", []) + ["No repository info for architect worker"],
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

    # If simple, add implementation instructions directly
    extra_instructions = ""
    if project_complexity == "simple":
        extra_instructions = """
5.  **Implement Business Logic (SIMPLE PROJECT)**:
    - Since this is a simple project, please implement the business logic immediately.
    - Connect the generated API routers to your implementation.
    - Ensure tests pass.
    - THIS IS THE FINAL STEP, so make sure it works.
"""

    task_content = f"""# Project: {project_spec.get("name", "project")}

## Description
{project_spec.get("description", "No description provided")}

## Requirements
- Modules: {", ".join(project_spec.get("modules", []))}
- Entry Points: {", ".join(project_spec.get("entry_points", []))}

## Task
Initialize this repository using the `service-template` framework.

1.  **Initialize Project via Copier**:
    - The template is located at `gh:vladmesh/service-template`.
    - Use `copier` to generate the project structure.
    - Run: `copier copy gh:vladmesh/service-template . \
      --data project_name={project_spec.get("name", "project")} \
      --data modules={",".join(project_spec.get("modules", ["backend"]))} \
      --trust` (adjust modules as needed).
    - If `copier` is not installed, install it: `pip install copier`.

2.  **Define Domain Specifications**:
    - Create YAML specifications in `shared/spec/` (or `domains/` if you prefer,
      but template uses `shared/spec`).
    - Define entities, aggregates, and services.

3.  **Setup Configuration**:
    - Ensure `.env` is created from `.env.example`.
    - Ensure `docker-compose.yml` is present (generated by copier).

4.  **Push Changes**:
    - You **MUST** commit and push your changes to the repository.
    - Run: `git add .`
    - Run: `git commit -m "Initial project structure"` (if not already committed)
    - Run: `git push`

{extra_instructions}

## Important
- **DO NOT** create a manual structure. You **MUST** use the `service-template`.
- Read `AGENTS.md` (if available in template) for context.
- All code should be async-ready (Python 3.12+).

## Commit Message
Initial project structure for {project_spec.get("name", "project")} using service-template
"""

    logger.info(f"Spawning Factory worker for {repo_full_name}")

    result = await request_spawn(
        repo=repo_full_name,
        github_token=token,
        task_content=task_content,
        task_title=f"Initial structure for {project_spec.get('name', 'project')}",
        model=os.getenv("FACTORY_MODEL", "claude-sonnet-4-5-20250929"),
    )

    if result.success:
        message = f"""✅ Project structure created successfully!

Repository: {repo_info.get("html_url")}
Commit: {result.commit_sha or "N/A"}

The repository now has:
- Domain specifications
- Project configuration
- Docker setup
- CI/CD workflow

Next step: Developer agent will implement business logic.
"""
        return {
            "messages": [AIMessage(content=message)],
            "architect_complete": True,
        }
    else:
        return {
            "messages": [AIMessage(content=f"❌ Factory worker failed:\n\n{result.output[-500:]}")],
            "errors": state.get("errors", []) + ["Factory worker failed"],
        }
