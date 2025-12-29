"""Developer agent node.

Orchestrates the implementation of business logic by spawning a Factory.ai worker.
This node runs after the Architect has set up the initial project structure.
"""

import json

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.worker_spawner import request_spawn
from .base import BaseAgentNode

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


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
        repo_info = state.get("repo_info") or {}
        project_spec = state.get("project_spec") or {}

        if not repo_info:
            return {
                "messages": [
                    AIMessage(content="❌ No repository info found. Cannot spawn developer worker.")
                ]
            }

        repo_full_name = repo_info.get("full_name", "")
        repo_name = repo_info.get("name") or repo_full_name.split("/")[-1]

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

        logger.info("developer_worker_spawn_requested", repo_name=repo_name)

        try:
            # Get GitHub App installation token for authentication
            github_client = GitHubAppClient()

            if "/" not in repo_full_name:
                return {
                    "messages": [
                        AIMessage(
                            content=(
                                f"❌ Invalid repository full_name '{repo_full_name}'. "
                                "Expected 'org/repo'."
                            )
                        )
                    ]
                }

            owner, repo = repo_full_name.split("/", 1)
            access_token = await github_client.get_token(owner, repo)

            task_content = project_spec.get("description") or "Implement business logic."
            if project_spec:
                task_content += "\n\nProject spec:\n"
                task_content += json.dumps(project_spec, indent=2, ensure_ascii=True)

            task_title = f"Implement business logic for {project_spec.get('name', repo_name)}"

            # Spawn the worker
            worker_result = await request_spawn(
                repo=repo_full_name,
                github_token=access_token,
                task_content=task_content,
                task_title=task_title,
            )

            if worker_result.success:
                return {
                    "messages": [
                        AIMessage(
                            content=f"✅ Developer worker spawned successfully for {repo_name}!\n"
                            f"Commit: {worker_result.commit_sha or 'N/A'}\n"
                            f"Branch: {worker_result.branch or 'N/A'}"
                        )
                    ],
                    "worker_info": worker_result,
                }
            else:
                # Check error_message first, then output, then logs_tail
                error_msg = (
                    worker_result.error_message
                    or worker_result.output
                    or worker_result.logs_tail
                    or "Unknown error"
                )
                # Truncate long error messages for Telegram
                if len(error_msg) > MAX_ERROR_MSG_LENGTH:
                    error_msg = error_msg[:MAX_ERROR_MSG_LENGTH] + "..."
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
                repo_name=repo_name,
                exc_info=True,
            )
            return {
                "messages": [AIMessage(content=f"❌ Error spawning developer worker: {str(e)}")]
            }


# Export singleton instance
developer_node = DeveloperNode()
