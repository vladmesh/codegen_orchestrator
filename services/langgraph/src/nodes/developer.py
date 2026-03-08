"""Unified Developer node.

Spawns a Claude Code worker to implement business logic. For new projects
(action=create), builds a ScaffoldConfig so the worker-manager runs copier +
make setup inside the container. For feature/fix actions on existing projects,
works directly with the existing repository.
"""

import os
import re

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.queues.worker import ScaffoldConfig

from ..clients.api import api_client
from ..clients.worker_spawner import request_spawn
from ..config.constants import Timeouts
from .base import FunctionalNode

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


class DeveloperNode(FunctionalNode):
    """Developer node - implements business logic in projects.

    For new projects (action=create, status=scaffolding):
        1. Build ScaffoldConfig from project spec
        2. Spawn worker with scaffold_config (worker-manager runs copier + make setup)
        3. Worker implements business logic according to /home/worker/TASK.md
        4. Update project status to scaffolded

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

        action = state.get("action", "create")
        feature_description = state.get("description")
        project_id = project_spec.get("id")

        logger.info(
            "developer_node_start",
            project_name=project_name,
            modules=modules,
            action=action,
        )

        # Build ScaffoldConfig for new projects in scaffolding status
        scaffold_config = self._build_scaffold_config(project_spec, action)

        # HARD FAIL: action=create + status=draft means scaffold was supposed to run
        # but didn't (stale in-memory project dict). Refuse to spawn on empty repo.
        # status=scaffolded/developing/deployed → scaffold already ran, proceed normally.
        if action == "create" and scaffold_config is None:
            project_status = project_spec.get("status", "unknown")
            if project_status == "draft":
                logger.error(
                    "scaffold_required_but_missing",
                    project_name=project_name,
                    project_status=project_status,
                    action=action,
                    hint="action=create + status='draft' means the project dict was not "
                    "refreshed after _create_repo_and_set_secrets() set DB status "
                    "to 'scaffolding'. This is a bug in engineering_worker.",
                )
                return {
                    "messages": [
                        AIMessage(
                            content="FATAL: action=create but project status is still 'draft'. "
                            "Scaffold phase cannot trigger — refusing to spawn worker on empty "
                            "repo. Check that engineering_worker refreshes the project dict "
                            "after _create_repo_and_set_secrets()."
                        )
                    ],
                    "engineering_status": "blocked",
                    "errors": state.get("errors", [])
                    + [
                        "Scaffold required but project status is 'draft' "
                        "(expected 'scaffolding'). Stale project_spec in state."
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
            repo_details = self._determine_repository(git_url, project_name)
            repo_full_name = repo_details["full_name"]
            owner = repo_details["owner"]
            repo_name = repo_details["name"]

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
            )

            # Build task title based on action
            task_title = self._get_task_title(action, project_name)

            # Spawn worker to implement business logic
            worker_result = await request_spawn(
                repo=repo_full_name,
                github_token=access_token,
                task_content=task_message,
                task_title=task_title,
                timeout_seconds=Timeouts.WORKER_SPAWN,
                project_id=project_id,
                scaffold_config=scaffold_config,
            )

            # Update project status based on scaffold result
            if scaffold_config and project_id:
                if worker_result.success:
                    await api_client.patch(
                        f"projects/{project_id}",
                        json={"status": "scaffolded"},
                    )
                else:
                    await api_client.patch(
                        f"projects/{project_id}",
                        json={"status": "scaffold_failed"},
                    )
                    error_msg = worker_result.error_message or "Scaffold phase failed"
                    return {
                        "messages": [AIMessage(content=f"Scaffolding failed: {error_msg}")],
                        "engineering_status": "blocked",
                        "errors": state.get("errors", []) + [f"Scaffold failed: {error_msg}"],
                    }

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
                                content=f"Worker completed but made no commit"
                                f" in '{project_name}'."
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

    def _build_scaffold_config(self, project_spec: dict, action: str) -> ScaffoldConfig | None:
        """Build ScaffoldConfig for new projects that need scaffolding.

        Returns ScaffoldConfig if scaffolding is needed, None otherwise.
        """
        project_status = project_spec.get("status", "draft")

        if action != "create" or project_status != "scaffolding":
            logger.info(
                "scaffold_config_decision",
                result="skip",
                action=action,
                project_status=project_status,
                reason="action != 'create'"
                if action != "create"
                else f"project_status='{project_status}' != 'scaffolding'",
            )
            return None

        config = project_spec.get("config") or {}
        modules = config.get("modules", ["backend"])
        modules_str = ",".join(modules)

        # Sanitize project name for copier
        project_name = project_spec.get("name", "project")
        sanitized = project_name.lower().replace("_", "-").replace(" ", "-")
        sanitized = re.sub(r"[^a-z0-9-]", "", sanitized)
        sanitized = re.sub(r"-+", "-", sanitized).strip("-")
        if not sanitized or not sanitized[0].isalpha():
            sanitized = "project-" + sanitized

        # Task description from config
        task_description = config.get("description", "")
        if not task_description:
            task_description = config.get("detailed_spec", "")

        template_repo = os.getenv(
            "SERVICE_TEMPLATE_REPO", "gh:project-factory-organization/service-template"
        )

        logger.info(
            "scaffold_config_decision",
            result="will_scaffold",
            action=action,
            project_status=project_status,
            template=template_repo,
            project_name=sanitized,
            modules=modules_str,
        )

        return ScaffoldConfig(
            template_repo=template_repo,
            project_name=sanitized,
            modules=modules_str,
            task_description=task_description,
        )

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
            )

        return self._build_create_task(
            project_name=project_name,
            description=description,
            modules=modules,
            project_spec=project_spec,
            feature_description=feature_description,
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
"""

    def _build_feature_task(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        action: str,
        feature_description: str | None,
        project_spec: dict,
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
"""


# Export singleton instance
developer_node = DeveloperNode()
