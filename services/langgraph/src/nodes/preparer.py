"""Preparer node for project structure initialization.

This node spawns a lightweight container that:
1. Runs copier with service-template
2. Writes TASK.md and AGENTS.md
3. Commits and pushes to the repository

No LLM is involved - this is a deterministic FunctionalNode.
"""

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.preparer_spawner import request_preparer
from ..templates import render_agents_md, render_task_md
from .base import FunctionalNode, log_node_execution

logger = structlog.get_logger()


class PreparerNode(FunctionalNode):
    """Spawns lightweight container to prepare project structure."""

    def __init__(self):
        super().__init__(node_id="preparer", timeout_seconds=120)

    @log_node_execution("preparer")
    async def run(self, state: dict) -> dict:
        """Prepare project structure via copier.

        Reads from state:
            - repo_info: Repository details (clone_url, full_name)
            - selected_modules: List of modules to include
            - project_spec: Project specification from Analyst
            - custom_task_instructions: Optional custom instructions

        Returns state updates:
            - repo_prepared: True if successful
            - preparer_commit_sha: Commit SHA from preparer
            - messages: Status message
        """
        repo_info = state.get("repo_info") or {}
        selected_modules = state.get("selected_modules") or ["backend"]
        project_spec = state.get("project_spec") or {}
        custom_instructions = state.get("custom_task_instructions", "")
        # deployment_hints are available in state but not used by preparer
        # They will be used later by DevOps node for deployment configuration

        # Validate required state
        if not repo_info:
            return {
                "messages": [AIMessage(content="Preparer: No repository info found")],
                "errors": state.get("errors", []) + ["No repository info for preparer"],
                "repo_prepared": False,
            }

        repo_full_name = repo_info.get("full_name")
        clone_url = repo_info.get("clone_url")

        if not repo_full_name or not clone_url:
            return {
                "messages": [AIMessage(content="Preparer: Repository info incomplete")],
                "errors": state.get("errors", []) + ["Repository info incomplete"],
                "repo_prepared": False,
            }

        # Get GitHub token
        github_client = GitHubAppClient()
        owner, repo = repo_full_name.split("/")

        try:
            token = await github_client.get_token(owner, repo)
        except Exception as e:
            logger.error(
                "preparer_github_token_failed",
                error=str(e),
                error_type=type(e).__name__,
            )
            return {
                "messages": [AIMessage(content=f"Preparer: Failed to get GitHub token: {e}")],
                "errors": state.get("errors", []) + [str(e)],
                "repo_prepared": False,
            }

        # Extract project info
        project_name = project_spec.get("name") or repo_info.get("name") or "project"
        description = project_spec.get("description", "")
        detailed_spec = project_spec.get("detailed_spec", "")

        # Render templates
        task_md = render_task_md(
            project_name=project_name,
            description=description,
            detailed_spec=detailed_spec,
            modules=selected_modules,
            custom_instructions=custom_instructions,
        )

        agents_md = render_agents_md(
            project_name=project_name,
            modules=selected_modules,
        )

        logger.info(
            "preparer_spawning",
            repo=repo_full_name,
            modules=selected_modules,
            project_name=project_name,
        )

        # Spawn preparer container
        result = await request_preparer(
            repo_url=clone_url,
            project_name=project_name,
            modules=selected_modules,
            github_token=token,
            task_md=task_md,
            agents_md=agents_md,
            timeout_seconds=self.timeout_seconds or 120,
        )

        if result.success:
            message = f"""Project structure prepared successfully!

Repository: {repo_info.get("html_url")}
Commit: {result.commit_sha or "N/A"}
Modules: {", ".join(selected_modules)}

The repository now has:
- Service-template structure with selected modules
- TASK.md with developer instructions
- AGENTS.md with framework guidelines

Next step: Developer agent will implement business logic.
"""
            logger.info(
                "preparer_complete",
                repo=repo_full_name,
                commit_sha=result.commit_sha,
            )

            return {
                "messages": [AIMessage(content=message)],
                "repo_prepared": True,
                "preparer_commit_sha": result.commit_sha,
                "current_agent": "preparer",
            }
        else:
            error_output = result.output[-1000:] if result.output else "No output"
            logger.error(
                "preparer_failed",
                repo=repo_full_name,
                exit_code=result.exit_code,
                error=result.error_message,
            )

            return {
                "messages": [AIMessage(content=f"Preparer failed:\n\n{error_output}")],
                "errors": state.get("errors", []) + ["Preparer container failed"],
                "repo_prepared": False,
            }


# Create singleton instance
_preparer_node = PreparerNode()
run = _preparer_node.run
