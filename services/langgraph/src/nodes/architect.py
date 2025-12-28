"""Architect agent node.

Creates project structure using Factory.ai Droid in an isolated container.
Generates high-level architecture without business logic.

Uses LLMNode for dynamic prompt loading from database.
"""

from typing import Any

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.worker_spawner import request_spawn
from ..tools.github import create_file_in_repo, create_github_repo, get_github_token
from .base import FactoryNode, LLMNode, log_node_execution

logger = structlog.get_logger()


@tool
def set_project_complexity(complexity: str):
    """Set the project complexity level.

    Args:
        complexity: "simple" or "complex"
    """
    return complexity


# Tools available to architect
tools = [create_github_repo, create_file_in_repo, get_github_token, set_project_complexity]


class ArchitectNode(LLMNode):
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


@log_node_execution("architect")
async def run(state: dict) -> dict:
    """Run architect agent.

    Creates GitHub repo and prepares for code generation.
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec") or {}
    allocated_resources = state.get("allocated_resources") or {}

    # Debug: log what project_spec we received
    logger.info(
        "architect_run_project_spec",
        project_spec_exists=bool(project_spec),
        project_spec_type=type(state.get("project_spec")).__name__,
        project_spec_name=project_spec.get("name") if project_spec else None,
        project_spec_keys=list(project_spec.keys()) if project_spec else None,
        raw_project_spec=state.get("project_spec"),
    )

    # Build context for LLM
    project_info = f"""
Name: {project_spec.get("name", "unknown")}
Description: {project_spec.get("description", "No description")}
Modules: {project_spec.get("modules", [])}
Entry Points: {project_spec.get("entry_points", [])}

Detailed Spec:
{project_spec.get("detailed_spec", "N/A")}
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


@log_node_execution("architect_tools")
async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Architect LLM.

    Delegates to LLMNode.execute_tools which handles:
    - Tool execution with error handling
    - State updates via handle_tool_result (repo_info, project_complexity)
    """
    return await _node.execute_tools(state)


class ArchitectWorkerNode(FactoryNode):
    """CLI node that spawns Factory.ai worker for project scaffolding."""

    def __init__(self):
        super().__init__(node_id="architect.spawn_factory_worker")

    @log_node_execution("architect_spawn_worker")
    async def run(self, state: dict) -> dict:
        """Spawn Factory.ai worker to generate project structure.

        This node is called after repo is created and token is obtained.
        It spawns a Sysbox container with Factory.ai Droid to generate code.
        """
        repo_info = state.get("repo_info") or {}
        project_spec = state.get("project_spec") or {}
        project_complexity = state.get("project_complexity", "complex")  # Default to complex

        if not repo_info:
            return {
                "messages": [
                    AIMessage(content="❌ No repository info found. Cannot spawn worker.")
                ],
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
            logger.error(
                "github_token_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "messages": [AIMessage(content=f"❌ Failed to get GitHub token: {e}")],
                "errors": state.get("errors", []) + [str(e)],
            }

        config = await self.get_config()
        prompt_template = config["prompt_template"]

        # If simple, add implementation instructions directly
        extra_instructions = ""
        if project_complexity == "simple":
            extra_instructions = (
                "5.  **Implement Business Logic (SIMPLE PROJECT)**:\n"
                "    - Since this is a simple project, please implement the business logic "
                "immediately.\n"
                "    - Connect the generated API routers to your implementation.\n"
                "    - Ensure tests pass.\n"
                "    - THIS IS THE FINAL STEP, so make sure it works.\n"
            )

        project_name = project_spec.get("name", "project")
        description = project_spec.get("description", "No description provided")
        modules = project_spec.get("modules") or []
        entry_points = project_spec.get("entry_points") or []

        prompt_vars = {
            "project_name": project_name,
            "description": description,
            "modules": ", ".join(modules),
            "entry_points": ", ".join(entry_points),
            "modules_csv": ",".join(modules or ["backend"]),
            "extra_instructions": extra_instructions,
            "task_instructions": "",
        }

        task_content = prompt_template.format(**prompt_vars)

        model_name = config.get("model_name")
        if not model_name:
            raise RuntimeError(
                "CLI agent config 'architect.spawn_factory_worker' missing model_name"
            )
        timeout_seconds = config.get("timeout_seconds") or 600

        logger.info("spawning_factory_worker", repo=repo_full_name)

        result = await request_spawn(
            repo=repo_full_name,
            github_token=token,
            task_content=task_content,
            task_title=f"Initial structure for {project_spec.get('name', 'project')}",
            model=model_name,
            timeout_seconds=timeout_seconds,
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
                "messages": [
                    AIMessage(content=f"❌ Factory worker failed:\n\n{result.output[-500:]}")
                ],
                "errors": state.get("errors", []) + ["Factory worker failed"],
            }


_worker_node = ArchitectWorkerNode()
spawn_factory_worker = _worker_node.run
