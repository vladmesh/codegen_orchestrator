"""Unified Developer node.

Spawns a Claude Code worker to implement business logic. For new projects
(action=create), the scaffolder service has already prepared the workspace.
Worker-manager mounts it by repo_id. For feature/fix actions on existing
projects, works directly with the existing repository.
"""

import json

from langchain_core.messages import AIMessage
from pydantic import ValidationError
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.engineering_attempt import FactoryResultEvidence
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.queues.worker import AgentType, WorkerOwnership

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
                "engineering_status": EngineeringStatus.FAILED,
                "errors": state.get("errors", []) + ["No project specification"],
            }

        project_name = project_spec.get("title") or project_spec.get("name", "project")
        config = project_spec.get("config") or {}
        project_description = config.get("description", "")
        modules = config.get("modules", ["backend"])

        # Agent type from project config (default: claude)
        agent_type_str = config.get("agent_type", "claude")
        try:
            agent_type = AgentType(agent_type_str)
        except ValueError:
            error = f"Unknown developer agent_type: {agent_type_str!r}"
            logger.error("unknown_developer_agent_type", agent_type=agent_type_str)
            return {
                "messages": [AIMessage(content=error)],
                "engineering_status": EngineeringStatus.FAILED,
                "errors": state.get("errors", []) + [error],
            }

        action = state.get("action", "create")
        feature_description = state.get("description")
        project_id = project_spec.get("id")
        # Who the worker this node is about to ask for belongs to. It was
        # decided before this node ran — the consumer built it from the message
        # that started the work — and it is read, never rebuilt: one fact, one
        # writer. A state without it is a bug, and KeyError says so here rather
        # than a container carrying an empty label later.
        ownership = state["ownership"]

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
                project_spec = fresh.model_dump()

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
                ownership=ownership,
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
                "engineering_status": EngineeringStatus.FAILED,
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
            "engineering_status": EngineeringStatus.FAILED,
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
        ownership: WorkerOwnership,
    ) -> dict:
        """Resolve repo, spawn (or reuse) worker, return state update."""
        # Get repository URL from Repository entity
        primary_repo = await api_client.get_primary_repository(project_id) if project_id else None
        git_url = primary_repo.git_url if primary_repo else None
        repo_id = primary_repo.id if primary_repo else None
        repo_details = self._determine_repository(git_url, project_name, project_spec.get("slug"))
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
            "ownership": ownership,
            "repo_id": str(repo_id) if repo_id else None,
            "agent_type": agent_type,
            "story_md": story_md,
            "branch": branch,
        }
        worker_result = await self._get_worker_result(
            state=state,
            spawn_kwargs=spawn_kwargs,
            project_name=project_name,
        )

        unpushed = await self._unpushed_commit_error(
            github_client=github_client,
            owner=owner,
            repo_name=repo_name,
            branch=branch,
            worker_result=worker_result,
        )
        if unpushed:
            logger.error(
                "developer_node_commit_not_on_origin",
                project_name=project_name,
                branch=branch,
                commit_sha=worker_result.commit_sha,
            )
            return {
                "messages": [AIMessage(content=unpushed)],
                "engineering_status": EngineeringStatus.FAILED,
                "errors": state.get("errors", []) + [unpushed],
            }

        return self._build_result_state(worker_result, project_name, repo_full_name, state)

    @staticmethod
    async def _unpushed_commit_error(
        *,
        github_client: GitHubAppClient,
        owner: str,
        repo_name: str,
        branch: str | None,
        worker_result,
    ) -> str | None:
        """Message describing an unpushed commit, or None when the commit is on origin.

        A worker reports the SHA it committed inside its container. If the push was
        rejected the commit never reaches GitHub, and every later stage — PR creation,
        merge, deploy — fails far from the cause. Confirm the SHA landed before calling
        engineering successful.
        """
        if not (worker_result.success and worker_result.commit_sha and branch):
            return None
        on_origin = await github_client.branch_contains_commit(
            owner, repo_name, branch, worker_result.commit_sha
        )
        if on_origin:
            return None
        return (
            f"Worker reported commit {worker_result.commit_sha} but it is not on "
            f"origin/{branch} — the push did not land."
        )

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
                ownership=spawn_kwargs["ownership"],
                story_md=spawn_kwargs["story_md"],
                branch=spawn_kwargs["branch"],
            )
            # A timeout requests teardown, but it does not prove that the
            # previous container is gone.  Do not create a sibling worker;
            # the worker-manager's owner-fenced workspace lock and supervisor
            # removal reconciliation decide when another attempt may start.
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
                    "engineering_status": EngineeringStatus.FAILED,
                    "errors": state.get("errors", [])
                    + ["Worker reported success but no commit was made"],
                    "worker_observability": DeveloperNode._worker_observability(
                        worker_result, state.get("project_spec") or {}
                    ),
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
                "engineering_status": EngineeringStatus.DONE,
                "commit_sha": worker_result.commit_sha,
                "worker_id": worker_result.worker_id,
                "worker_report": worker_result.worker_report,
                "worker_observability": DeveloperNode._worker_observability(
                    worker_result, state.get("project_spec") or {}
                ),
            }

        if worker_result.gave_up_reason:
            logger.warning(
                "developer_node_gave_up",
                project_name=project_name,
                gave_up_reason=worker_result.gave_up_reason[:200],
            )
            return {
                "messages": [AIMessage(content=f"Worker gave up: {worker_result.gave_up_reason}")],
                "engineering_status": EngineeringStatus.GAVE_UP,
                "gave_up_reason": worker_result.gave_up_reason,
                "worker_id": worker_result.worker_id,
                "worker_report": worker_result.worker_report,
                "worker_observability": DeveloperNode._worker_observability(
                    worker_result, state.get("project_spec") or {}
                ),
                "errors": state.get("errors", [])
                + [f"Worker gave up: {worker_result.gave_up_reason}"],
            }

        error_msg = worker_result.error_message or worker_result.output or "Unknown error"
        if len(error_msg) > MAX_ERROR_MSG_LENGTH:
            error_msg = error_msg[:MAX_ERROR_MSG_LENGTH] + "..."

        logger.error(
            "developer_node_failed",
            project_name=project_name,
            error=error_msg,
            stop_reason=worker_result.stop_reason.value if worker_result.stop_reason else None,
            agent_limit_seconds=worker_result.agent_limit_seconds,
        )
        return {
            "messages": [AIMessage(content=f"Development failed:\n{error_msg}")],
            "engineering_status": EngineeringStatus.FAILED,
            "errors": state.get("errors", []) + [f"Development failed: {error_msg}"],
            # Why the turn stopped, when the worker said why. It travels to the
            # attempt's run_metadata so a failed run is readable as "ran out of
            # its limit" rather than as an unexplained failure.
            "stop_reason": worker_result.stop_reason,
            "agent_limit_seconds": worker_result.agent_limit_seconds,
            "worker_observability": DeveloperNode._worker_observability(
                worker_result, state.get("project_spec") or {}
            ),
        }

    @staticmethod
    def _worker_observability(worker_result, project_spec: dict) -> dict:
        """Keep provider metrics separate from the strict engineering result."""
        config = project_spec.get("config") or {}
        agent_type = str(config.get("agent_type", "claude"))
        provider_by_agent = {"claude": "anthropic", "codex": "openai", "factory": "factory"}
        claude_evidence = worker_result.claude_evidence
        if claude_evidence is not None:
            return {
                key: value
                for key, value in {
                    "claude_evidence": claude_evidence.model_dump(mode="json"),
                    "transcript_path": worker_result.transcript_path,
                    "transcript_truncated": worker_result.transcript_truncated,
                    "agent_profile": {
                        "agent_type": agent_type,
                        "provider": claude_evidence.provider,
                        "model": claude_evidence.model,
                        "adapter": "worker-wrapper",
                    },
                }.items()
                if value is not None
            }
        factory_evidence = worker_result.factory_evidence
        if factory_evidence is not None:
            configured_model = config.get("model_identifier") or config.get("model_name")
            if factory_evidence.model is None and isinstance(configured_model, str):
                try:
                    factory_evidence = FactoryResultEvidence.model_validate(
                        factory_evidence.model_dump() | {"model": configured_model}
                    )
                except ValidationError:
                    pass
            return {
                key: value
                for key, value in {
                    "factory_evidence": factory_evidence.model_dump(mode="json"),
                    "transcript_path": worker_result.transcript_path,
                    "transcript_truncated": worker_result.transcript_truncated,
                    "agent_profile": {
                        "agent_type": agent_type,
                        "provider": factory_evidence.provider,
                        "model": factory_evidence.model,
                        "adapter": "worker-wrapper",
                    },
                }.items()
                if value is not None
            }
        return {
            key: value
            for key, value in {
                "input_tokens": worker_result.input_tokens,
                "output_tokens": worker_result.output_tokens,
                "total_tokens": worker_result.total_tokens,
                "transcript_path": worker_result.transcript_path,
                "transcript_truncated": worker_result.transcript_truncated,
                "agent_profile": {
                    "agent_type": agent_type,
                    "provider": config.get("llm_provider") or provider_by_agent.get(agent_type),
                    "model": (
                        DeveloperNode._reported_model(worker_result.logs_tail)
                        if agent_type == "claude"
                        else None
                    )
                    or config.get("model_identifier")
                    or config.get("model_name"),
                    "adapter": "worker-wrapper",
                },
            }.items()
            if value is not None
        }

    @staticmethod
    def _reported_model(agent_stdout: str | None) -> str | None:
        """Read the model identifier from a Claude JSON result when it is present."""
        if not agent_stdout:
            return None
        try:
            payload = json.loads(agent_stdout.split("\n--- stderr ---\n", maxsplit=1)[0])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if isinstance(model, str):
            return model
        model_usage = payload.get("modelUsage")
        if isinstance(model_usage, dict):
            return next((name for name in model_usage if isinstance(name, str)), None)
        return None

    # ------------------------------------------------------------------
    # Thin delegations (keeps tests calling node._method_name working)
    # ------------------------------------------------------------------

    def _determine_repository(
        self, git_url: str | None, project_name: str, project_slug: str | None = None
    ) -> dict:
        return determine_repository(git_url, project_name, project_slug)

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
