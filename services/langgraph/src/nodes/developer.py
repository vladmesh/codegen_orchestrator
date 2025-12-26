"""Developer agent node.

Orchestrates the implementation of business logic by spawning a Factory.ai worker.
This node runs after the Architect has set up the initial project structure.
"""

import logging

from langchain_core.messages import AIMessage

from shared.clients.github import GitHubAppClient

from ..clients.worker_spawner import request_spawn
from .base import BaseAgentNode

logger = logging.getLogger(__name__)


class DeveloperNode(BaseAgentNode):
    """Developer agent - spawns Factory.ai workers for code implementation.

    This node doesn't use LLM tools directly, but inherits from BaseAgentNode
    for consistency with other agents. The actual coding work is delegated to
    Factory.ai workers.
    """

    def __init__(self):
        """Initialize Developer node."""
        super().__init__(agent_id="developer", tools=[])

    async def run(self, state: dict) -> dict:
        """Run developer agent.

        Currently just a localized step to prepare for worker spawning.
        In the future, this could analyze the codebase before spawning.

        Args:
            state: Graph state

        Returns:
            Updated state with current agent marker
        """
        return {
            "current_agent": "developer",
        }

    async def spawn_worker(self, state: dict) -> dict:
        """Spawn Factory.ai worker to implement business logic.

        Args:
            state: Graph state with repo_info and project_spec

        Returns:
            Updated state with worker spawn result
        """
        repo_info = state.get("repo_info", {})
        project_spec = state.get("project_spec", {})

        if not repo_info:
            return {
                "messages": [
                    AIMessage(content="❌ No repository info found. Cannot spawn developer worker.")
                ]
            }

        repo_name = repo_info.get("name", "")
        repo_full_name = repo_info.get("full_name")

        if not repo_full_name:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"❌ No full_name for repository '{repo_name}'. Cannot spawn worker."
                        )
                    )
                ]
            }

        if "/" not in repo_full_name:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "❌ Invalid repository full_name "
                            f"'{repo_full_name}'. Expected 'org/repo'."
                        )
                    )
                ]
            }

        logger.info(f"Spawning developer worker for repository: {repo_name}")

        try:
            # Get GitHub App installation token for authentication
            github_client = GitHubAppClient()
            owner, repo = repo_full_name.split("/", 1)
            access_token = await github_client.get_token(owner, repo)

            modules = project_spec.get("modules") or []
            entry_points = project_spec.get("entry_points") or []
            description = project_spec.get("description") or "Implement business logic"

            # Spawn the worker
            worker_result = await request_spawn(
                repo=repo_full_name,
                github_token=access_token,
                task_content=(
                    f"# Project: {repo_name}\n\n"
                    f"## Description\n{description}\n\n"
                    "## Requirements\n"
                    f"- Modules: {', '.join(modules)}\n"
                    f"- Entry Points: {', '.join(entry_points)}\n\n"
                    "## Task\n"
                    "Implement the business logic according to the project specification.\n"
                ),
                task_title=f"Implement business logic for {repo_name}",
            )

            if worker_result.success:
                return {
                    "messages": [
                        AIMessage(
                            content=f"✅ Developer worker spawned successfully for {repo_name}!\n"
                            f"Request ID: {worker_result.request_id}"
                        )
                    ],
                    "worker_info": worker_result.__dict__,
                }
            else:
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                "❌ Failed to spawn developer worker:\n\n"
                                f"{worker_result.output[-500:]}"
                            )
                        )
                    ]
                }

        except Exception as e:
            logger.error(f"Error spawning developer worker: {e}", exc_info=True)
            return {
                "messages": [AIMessage(content=f"❌ Error spawning developer worker: {str(e)}")]
            }


# Export singleton instance
developer_node = DeveloperNode()
