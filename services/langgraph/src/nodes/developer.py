"""Unified Developer node.

Spawns a Claude Code worker to implement business logic. For new projects
(action=create), the scaffolder service has already prepared the workspace.
Worker-manager mounts it by repo_id. For feature/fix actions on existing
projects, works directly with the existing repository.
"""

import os

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.queues.worker import AgentType

from ..clients.api import api_client
from ..clients.worker_spawner import request_spawn, send_task_to_worker
from ..config.constants import Timeouts
from .base import FunctionalNode

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


class DeveloperNode(FunctionalNode):
    """Developer node - implements business logic in projects.

    For new projects (action=create, status=scaffolded):
        1. Scaffolder has already prepared workspace at /data/workspaces/{repo_id}
        2. Spawn worker with repo_id (worker-manager mounts pre-scaffolded workspace)
        3. Worker implements business logic according to /home/worker/TASK.md

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

        # For action=create, scaffolder must have already run (status=scaffolded).
        # Draft status means the pipeline didn't trigger scaffolder properly.
        if action == "create":
            project_status = project_spec.get("status", "unknown")
            if project_status == "draft":
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
                    + [
                        "Scaffold required but project status is 'draft'. "
                        "Scaffolder must run first."
                    ],
                }

        # Refresh project data
        if project_id:
            fresh = await api_client.get_project(project_id)
            if fresh:
                project_spec = fresh

        try:
            # Get repository URL from Repository entity
            primary_repo = (
                await api_client.get_primary_repository(project_id) if project_id else None
            )
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

            # Build task title based on action
            task_title = self._get_task_title(action, project_name)

            # Reuse existing worker if worker_id is in state (story-level reuse)
            existing_worker_id = state.get("worker_id")
            if existing_worker_id:
                logger.info(
                    "developer_reuse_worker",
                    worker_id=existing_worker_id,
                    project_name=project_name,
                )
                worker_result = await send_task_to_worker(
                    worker_id=existing_worker_id,
                    task_content=task_message,
                    timeout_seconds=Timeouts.WORKER_SPAWN,
                )
                # Fall back to fresh spawn if worker is dead
                if not worker_result.success and worker_result.error_message == "execution_timeout":
                    logger.warning(
                        "developer_reuse_failed_fallback",
                        worker_id=existing_worker_id,
                        project_name=project_name,
                    )
                    worker_result = await request_spawn(
                        repo=repo_full_name,
                        github_token=access_token,
                        task_content=task_message,
                        task_title=task_title,
                        timeout_seconds=Timeouts.WORKER_SPAWN,
                        project_id=project_id,
                        repo_id=repo_id,
                        agent_type=agent_type,
                    )
            else:
                # Spawn fresh worker
                worker_result = await request_spawn(
                    repo=repo_full_name,
                    github_token=access_token,
                    task_content=task_message,
                    task_title=task_title,
                    timeout_seconds=Timeouts.WORKER_SPAWN,
                    project_id=project_id,
                    repo_id=repo_id,
                    agent_type=agent_type,
                )

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
                }
            else:
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

    def _determine_repository(self, git_url: str | None, project_name: str) -> dict:
        """Determine repository details (owner, name, full_name)."""
        if git_url and "github.com/" in git_url:
            repo_full_name = git_url.split("github.com/")[-1].rstrip("/").removesuffix(".git")
            owner, repo_name = repo_full_name.split("/", 1)
            logger.info(
                "using_git_url_from_repository",
                git_url=git_url,
                repo_full_name=repo_full_name,
            )
        else:
            owner = os.getenv("GITHUB_ORG")
            if not owner:
                raise RuntimeError("No repository found for project and GITHUB_ORG env not set")
            repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
            repo_full_name = f"{owner}/{repo_name}"
            logger.info(
                "inferring_repo_from_project_name",
                project_name=project_name,
                repo_full_name=repo_full_name,
            )
        return {"owner": owner, "name": repo_name, "full_name": repo_full_name}

    def _get_task_title(self, action: str, project_name: str) -> str:
        """Get task title based on action."""
        if action == "feature":
            return f"Add feature to {project_name}"
        if action == "fix":
            return f"Fix issue in {project_name}"
        return f"Build {project_name}"

    def _build_task_message(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        repo_full_name: str,
        project_spec: dict,
        action: str = "create",
        feature_description: str | None = None,
        story_context: str | None = None,
    ) -> str:
        """Build TASK.md content for the developer worker.

        Contains only project-specific information. Generic role instructions
        are in services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md.

        For action=create: scaffolded project build task.
        For action=feature/fix: targeted change task for existing project.
        """
        if action in ("feature", "fix"):
            return self._build_feature_task(
                project_name=project_name,
                description=description,
                modules=modules,
                action=action,
                feature_description=feature_description,
                project_spec=project_spec,
                story_context=story_context,
            )

        return self._build_create_task(
            project_name=project_name,
            description=description,
            modules=modules,
            project_spec=project_spec,
            feature_description=feature_description,
            story_context=story_context,
        )

    def _format_env_hints(self, project_spec: dict) -> str:
        """Format env_hints from project config into a TASK.md section."""
        config = project_spec.get("config") or {}
        env_hints = config.get("env_hints") or {}
        if not env_hints:
            return ""

        lines = [
            "\n## Provided Environment Variables\n",
            "The Product Owner has already defined the following environment variables "
            "for this project.",
            "You MUST use them in your code via `os.getenv()` or `pydantic-settings`. "
            "Do NOT ask the user for them.\n",
        ]
        for key, hint in sorted(env_hints.items()):
            lines.append(f"- `{key}`: {hint}")
        lines.append("")
        return "\n".join(lines)

    def _build_create_task(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        project_spec: dict,
        feature_description: str | None = None,
        story_context: str | None = None,
    ) -> str:
        """Build task message for new project creation (scaffolded)."""
        modules_str = ",".join(modules)
        has_backend = "backend" in modules

        spec_lines = ""
        if has_backend:
            spec_lines = (
                "\n- `shared/spec/models.yaml` - domain models definition"
                "\n- `shared/spec/events.yaml` - events definition"
            )

        generate_hint = ""
        if has_backend:
            generate_hint = (
                "\nRun `make generate-from-spec` after modifying spec files to regenerate code.\n"
            )

        env_hints_section = self._format_env_hints(project_spec)

        detailed_spec = project_spec.get("detailed_spec") or feature_description or "N/A"

        return f"""# Task: Build {project_name}

## Project Specification

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}

**Detailed Spec**:
{detailed_spec}
{env_hints_section}
## Project Structure (already scaffolded)

The project was scaffolded with `copier` from `service-template`.
You'll find:
- `services/{modules_str.split(",")[0]}/` - main service directory{spec_lines}
- `/home/worker/TASK.md` - detailed requirements
- `AGENTS.md` - code structure patterns
- `Makefile` - build commands
{generate_hint}
## Implementation

Implement the business logic according to the specification:
- Read /home/worker/TASK.md for detailed requirements
- Follow patterns in AGENTS.md for code structure
- Implement all required functionality
- Use existing generated code as foundation
{self._format_story_context(story_context)}"""

    def _format_story_context(self, story_context: str | None) -> str:
        """Format story context section for task message. Empty string if no context."""
        if not story_context:
            return ""
        return f"""
## Story Context (Previous Work)

The following tasks were completed (or are in progress) as part of this story.
Use this context to understand what has already been done — do NOT redo completed work.

{story_context}"""

    def _build_feature_task(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        action: str,
        feature_description: str | None,
        project_spec: dict,
        story_context: str | None = None,
    ) -> str:
        """Build task message for feature addition or bug fix."""
        action_label = "Add Feature" if action == "feature" else "Fix Issue"
        task_description = feature_description or description or "No description provided"
        modules_str = ", ".join(modules)
        env_hints_section = self._format_env_hints(project_spec)

        return f"""# Task: {action_label} in {project_name}

## What To Do

{task_description}

## Project Context

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}
{env_hints_section}
## Important

- This is an **existing, working project** — do NOT regenerate or restructure it
- Read existing code to understand the architecture before making changes
- Make **targeted changes** — don't rewrite existing working code
- Keep changes minimal and focused on the task description
- Ensure all existing tests still pass after your changes
- Add tests for new functionality where appropriate
- Commit with descriptive message (e.g., "feat: add /stats command" or "fix: handle empty input")
{self._format_story_context(story_context)}"""


# Export singleton instance
developer_node = DeveloperNode()
