"""Developer agent node.

Orchestrates the implementation of business logic by spawning a Factory.ai worker.
This node runs after the Architect has set up the initial project structure.
"""

from langchain_core.messages import AIMessage
import time

import structlog

from ..clients.github import GitHubAppClient
from ..clients.worker_spawner import request_spawn
from .base import BaseAgentNode, log_node_execution

logger = structlog.get_logger()


class DeveloperNode(BaseAgentNode):
    """Developer agent - spawns Factory.ai workers for code implementation.

    This node doesn't use LLM tools directly, but inherits from BaseAgentNode
    for consistency with other agents. The actual coding work is delegated to
    Factory.ai workers.
    """

    def __init__(self):
        """Initialize Developer node."""
        super().__init__(agent_id="developer", tools=[])

    @log_node_execution("developer")
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

    @log_node_execution("developer_spawn_worker")
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
        repo_clone_url = repo_info.get("clone_url", "")

        if not repo_clone_url:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            f"❌ No clone URL for repository '{repo_name}'. Cannot spawn worker."
                        )
                    )
                ]
            }

        logger.info("spawning_developer_worker", repo_name=repo_name)

        try:
            # Get GitHub App installation token for authentication
            github_client = GitHubAppClient()
            installation_id = repo_info.get("installation_id")

            if not installation_id:
                return {
                    "messages": [
                        AIMessage(
                            content=f"❌ No GitHub App installation ID found for '{repo_name}'."
                        )
                    ]
                }

            access_token = await github_client.get_installation_token(installation_id)

            # Prepare clone URL with authentication
            authenticated_clone_url = repo_clone_url.replace(
                "https://", f"https://x-access-token:{access_token}@"
            )

            # Spawn the worker
            start = time.time()
            worker_result = await request_spawn(
                repository_url=authenticated_clone_url,
                task_description=project_spec.get("description", "Implement business logic"),
                context={
                    "project_name": repo_info.get("name"),
                    "spec": project_spec,
                },
            )
            duration_ms = (time.time() - start) * 1000

            logger.info(
                "worker_result_received",
                repo_name=repo_name,
                success=worker_result.get("success"),
                duration_ms=round(duration_ms, 2),
            )

            if worker_result.get("success"):
                return {
                    "messages": [
                        AIMessage(
                            content=f"✅ Developer worker spawned successfully for {repo_name}!\n"
                            f"Worker ID: {worker_result.get('worker_id', 'N/A')}"
                        )
                    ],
                    "worker_info": worker_result,
                }
            else:
                error_msg = worker_result.get("error", "Unknown error")
                return {
                    "messages": [
                        AIMessage(content=f"❌ Failed to spawn developer worker: {error_msg}")
                    ]
                }

        except Exception as e:
            logger.error(
                "developer_worker_spawn_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "messages": [AIMessage(content=f"❌ Error spawning developer worker: {str(e)}")]
            }


# Export singleton instance
developer_node = DeveloperNode()
