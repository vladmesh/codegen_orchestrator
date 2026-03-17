"""Unified Developer node.

Spawns a Claude Code worker to implement business logic. For new projects
(action=create), the scaffolder service has already prepared the workspace.
Worker-manager mounts it by repo_id. For feature/fix actions on existing
projects, works directly with the existing repository.
"""

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.worker import AgentType

from ..clients.api import api_client
from ..clients.worker_spawner import request_spawn, send_task_to_worker
from ..config.constants import Timeouts
from .base import FunctionalNode
from .developer_tasks import (
    build_create_task,
    build_feature_task,
    build_task_message,
    determine_repository,
    format_env_hints,
    format_story_context,
    get_task_title,
)

__all__ = [
    "DeveloperNode",
    "developer_node",
    "build_task_message",
    "build_create_task",
    "build_feature_task",
    "determine_repository",
    "format_env_hints",
    "format_story_context",
    "get_task_title",
]

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


class DeveloperNode(FunctionalNode):
    """Developer node - implements business logic in projects.

    For new projects (action=create, status=scaffolded):
        1. Scaffolder has already prepared workspace at /data/workspaces/{repo_id}
        2. Spawn worker with repo_id (worker-manager mounts pre-scaffolded workspace)
        3. Worker implements business logic according to TASK.md

    For existing projects (action=feature/fix):
        1. Spawn Claude Code worker to clone existing repo
        2. Implement changes according to task description
        3. Commit and push changes
    """

    def __init__(self):
        """Initialize Developer node."""
        super().__init__(node_id="developer")

    async def run(self, state: dict) -> dict:
        """Spawn worker and delegate all engineering work to Claude.

        Args:
            state: Graph state with project_spec and current_project

        Returns:
            Updated state with engineering result
        """
        project_spec = state.get("project_spec") or {}

        if not project_spec:
            return {
                "messages": [AIMessage(content="No project specification found.")],
                "engineering_status": "blocked",
                "errors": state.get("errors", []) + ["No project specification"],
            }

        project_name = project_spec.get("name", "project")
        config = project_spec.get("config") or {}
        project_description = config.get("description", "")
        modules = config.get("modules", ["backend"])

        # Agent type from project config (default: claude)
        agent_type_str = config.get("agent_type", "claude")
        try:
            agent_type = AgentType(agent_type_str)
        except ValueError:
            agent_type = AgentType.CLAUDE

        action = state.get("action", "create")
        feature_description = state.get("description")
        project_id = project_spec.get("id")

        logger.info(
            "developer_node_start",
            project_name=project_name,
            modules=modules,
            action=action,
        )

        # For action=create, scaffolder must have already run (project is active).
        # Draft status means the pipeline didn't trigger scaffolder properly.
        if action == "create":
            blocked = self._check_scaffold_required(project_spec, project_name, action, state)
            if blocked:
                return blocked

        # Refresh project data
        if project_id:
            fresh = await api_client.get_project(project_id)
            if fresh:
                project_spec = fresh

        try:
            return await self._spawn_and_collect(
                state=state,
                project_spec=project_spec,
                project_name=project_name,
                project_description=project_description,
                modules=modules,
                agent_type=agent_type,
                action=action,
                feature_description=feature_description,
                project_id=project_id,
            )
        except Exception as e:
            logger.error(
                "developer_node_exception",
                project_name=project_name,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "messages": [AIMessage(content=f"Error in developer node: {str(e)}")],
                "engineering_status": "blocked",
                "errors": state.get("errors", []) + [f"Developer error: {str(e)}"],
            }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_scaffold_required(
        project_spec: dict, project_name: str, action: str, state: dict
    ) -> dict | None:
        """Return an error state dict if scaffold is missing, else None."""
        project_status = project_spec.get("status", "unknown")
        if project_status != ProjectStatus.DRAFT.value:
            return None
        logger.error(
            "scaffold_required_but_missing",
            project_name=project_name,
            project_status=project_status,
            action=action,
        )
        return {
            "messages": [
                AIMessage(
                    content="FATAL: action=create but project status is still 'draft'. "
                    "Scaffolder must run before developer. Check pipeline."
                )
            ],
            "engineering_status": "blocked",
            "errors": state.get("errors", [])
            + ["Scaffold required but project status is 'draft'. Scaffolder must run first."],
        }

    async def _spawn_and_collect(
        self,
        *,
        state: dict,
        project_spec: dict,
        project_name: str,
        project_description: str,
        modules: list[str],
        agent_type: AgentType,
        action: str,
        feature_description: str | None,
        project_id: str | None,
    ) -> dict:
        """Resolve repo, spawn (or reuse) worker, return state update."""
        # Get repository URL from Repository entity
        primary_repo = await api_client.get_primary_repository(project_id) if project_id else None
        git_url = primary_repo.get("git_url") if primary_repo else None
        repo_id = primary_repo.get("id") if primary_repo else None
        repo_details = self._determine_repository(git_url, project_name)
        repo_full_name = repo_details["full_name"]
        owner = repo_details["owner"]
        repo_name = repo_details["name"]

        # Also check state for repo_id (passed from engineering consumer)
        if not repo_id:
            repo_id = state.get("repo_id")

        # Get GitHub App token
        github_client = GitHubAppClient()
        access_token = await github_client.get_token(owner, repo_name)

        # Build comprehensive task message for Claude
        task_message = self._build_task_message(
            project_name=project_name,
            description=project_description,
            modules=modules,
            repo_full_name=repo_full_name,
            project_spec=project_spec,
            action=action,
            feature_description=feature_description,
            story_context=state.get("story_context"),
        )

        task_title = self._get_task_title(action, project_name)
        story_md = state.get("story_md")
        branch = state.get("branch")

        # Spawn or reuse worker
        spawn_kwargs = {
            "repo": repo_full_name,
            "github_token": access_token,
            "task_content": task_message,
            "task_title": task_title,
            "timeout_seconds": Timeouts.WORKER_SPAWN,
            "project_id": project_id,
            "repo_id": repo_id,
            "agent_type": agent_type,
            "story_md": story_md,
            "branch": branch,
        }
        worker_result = await self._get_worker_result(
            state=state,
            spawn_kwargs=spawn_kwargs,
            project_name=project_name,
        )

        return self._build_result_state(worker_result, project_name, repo_full_name, state)

    async def _get_worker_result(
        self,
        *,
        state: dict,
        spawn_kwargs: dict,
        project_name: str,
    ):
        """Reuse existing worker or spawn a fresh one."""
        existing_worker_id = state.get("worker_id")
        if existing_worker_id:
            logger.info(
                "developer_reuse_worker",
                worker_id=existing_worker_id,
                project_name=project_name,
            )
            worker_result = await send_task_to_worker(
                worker_id=existing_worker_id,
                task_content=spawn_kwargs["task_content"],
                timeout_seconds=Timeouts.WORKER_SPAWN,
                story_md=spawn_kwargs["story_md"],
                branch=spawn_kwargs["branch"],
            )
            # Fall back to fresh spawn if worker is dead
            if not worker_result.success and worker_result.error_message == "execution_timeout":
                logger.warning(
                    "developer_reuse_failed_fallback",
                    worker_id=existing_worker_id,
                    project_name=project_name,
                )
                worker_result = await request_spawn(**spawn_kwargs)
        else:
            worker_result = await request_spawn(**spawn_kwargs)
        return worker_result

    @staticmethod
    def _build_result_state(
        worker_result, project_name: str, repo_full_name: str, state: dict
    ) -> dict:
        """Convert a WorkerResult into a graph state update dict."""
        if worker_result.success:
            if not worker_result.commit_sha:
                logger.error(
                    "developer_node_no_commit",
                    project_name=project_name,
                    output=worker_result.output[:500],
                )
                return {
                    "messages": [
                        AIMessage(
                            content=f"Worker completed but made no commit in '{project_name}'."
                        )
                    ],
                    "engineering_status": "blocked",
                    "errors": state.get("errors", [])
                    + ["Worker reported success but no commit was made"],
                }

            logger.info(
                "developer_node_success",
                project_name=project_name,
                commit_sha=worker_result.commit_sha,
                output_length=len(worker_result.output),
            )
            return {
                "messages": [
                    AIMessage(
                        content=f"Project '{project_name}' developed successfully!\n\n"
                        f"Repository: https://github.com/{repo_full_name}\n"
                        f"Output:\n{worker_result.output[:500]}"
                    )
                ],
                "engineering_status": "done",
                "commit_sha": worker_result.commit_sha,
                "worker_id": worker_result.worker_id,
                "worker_report": worker_result.worker_report,
            }

        if worker_result.reject_reason:
            logger.warning(
                "developer_node_worker_rejected",
                project_name=project_name,
                reject_reason=worker_result.reject_reason[:200],
            )
            return {
                "messages": [
                    AIMessage(content=f"Worker rejected task: {worker_result.reject_reason}")
                ],
                "engineering_status": "worker_rejected",
                "reject_reason": worker_result.reject_reason,
                "worker_id": worker_result.worker_id,
                "worker_report": worker_result.worker_report,
                "errors": state.get("errors", [])
                + [f"Worker rejected: {worker_result.reject_reason}"],
            }

        if worker_result.block_reason:
            logger.warning(
                "developer_node_blocked",
                project_name=project_name,
                block_reason=worker_result.block_reason[:200],
            )
            return {
                "messages": [AIMessage(content=f"Developer blocked: {worker_result.block_reason}")],
                "engineering_status": "developer_blocked",
                "block_reason": worker_result.block_reason,
                "worker_id": worker_result.worker_id,
                "worker_report": worker_result.worker_report,
                "errors": state.get("errors", [])
                + [f"Developer blocked: {worker_result.block_reason}"],
            }

        error_msg = worker_result.error_message or worker_result.output or "Unknown error"
        if len(error_msg) > MAX_ERROR_MSG_LENGTH:
            error_msg = error_msg[:MAX_ERROR_MSG_LENGTH] + "..."

        logger.error(
            "developer_node_failed",
            project_name=project_name,
            error=error_msg,
        )
        return {
            "messages": [AIMessage(content=f"Development failed:\n{error_msg}")],
            "engineering_status": "blocked",
            "errors": state.get("errors", []) + [f"Development failed: {error_msg}"],
        }

    # ------------------------------------------------------------------
    # Thin delegations (keeps tests calling node._method_name working)
    # ------------------------------------------------------------------

    def _determine_repository(self, git_url: str | None, project_name: str) -> dict:
        return determine_repository(git_url, project_name)

    def _get_task_title(self, action: str, project_name: str) -> str:
        return get_task_title(action, project_name)

    def _build_task_message(self, **kwargs) -> str:
        return build_task_message(**kwargs)

    def _build_create_task(self, **kwargs) -> str:
        return build_create_task(**kwargs)

    def _build_feature_task(self, **kwargs) -> str:
        return build_feature_task(**kwargs)

    def _format_env_hints(self, project_spec: dict) -> str:
        return format_env_hints(project_spec)

    def _format_story_context(self, story_context: str | None) -> str:
        return format_story_context(story_context)


# Export singleton instance
developer_node = DeveloperNode()
