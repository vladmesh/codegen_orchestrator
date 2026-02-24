"""Unified Developer node.

Waits for scaffolding completion (for new projects), then spawns a Claude Code
worker to implement business logic. For feature/fix actions on existing projects,
skips scaffolding and works directly with the existing repository.
"""

import asyncio
import os

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.api import api_client
from ..clients.worker_spawner import request_spawn
from ..config.constants import Timeouts
from .base import FunctionalNode

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


class DeveloperNode(FunctionalNode):
    """Developer node - implements business logic in projects.

    For new projects (action=create):
        1. Wait for project status == 'scaffolded' (done by scaffolder service)
        2. Spawn Claude Code worker to clone scaffolded repo
        3. Implement business logic according to /home/worker/TASK.md
        4. Commit and push changes

    For existing projects (action=feature/fix):
        1. Skip scaffolding wait (repo already exists)
        2. Spawn Claude Code worker to clone existing repo
        3. Implement changes according to task description
        4. Commit and push changes
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
                "messages": [AIMessage(content="❌ No project specification found.")],
                "engineering_status": "blocked",
                "errors": state.get("errors", []) + ["No project specification"],
            }

        project_name = project_spec.get("name", "project")
        project_description = project_spec.get("description", "")
        # Modules are stored in project.config.modules
        config = project_spec.get("config") or {}
        modules = config.get("modules", ["backend"])

        action = state.get("action", "create")
        feature_description = state.get("description")

        logger.info(
            "developer_node_start",
            project_name=project_name,
            modules=modules,
            action=action,
        )

        # Handle scaffolding wait if needed
        scaffold_result = await self._wait_for_scaffolding(project_spec, action, state)
        if scaffold_result:
            # If it returns a dict, it means error or updated project spec?
            # Let's adjust the signature. If it returns a dict with "engineering_status",
            # it's an error state.
            # If it returns a project dict, it's successful update.
            if "engineering_status" in scaffold_result:
                return scaffold_result
            # Otherwise it's the updated project_spec
            project_spec = scaffold_result

        try:
            # Determine repository details
            repo_details = self._determine_repository(project_spec, project_name)
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
            project_id = project_spec.get("id")
            worker_result = await request_spawn(
                repo=repo_full_name,
                github_token=access_token,
                task_content=task_message,
                task_title=task_title,
                timeout_seconds=Timeouts.WORKER_SPAWN,
                project_id=project_id,
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
                                content=f"❌ Worker completed but made no commit"
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
                            content=f"✅ Project '{project_name}' developed successfully!\n\n"
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
                    "messages": [AIMessage(content=f"❌ Development failed:\n{error_msg}")],
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
                "messages": [AIMessage(content=f"❌ Error in developer node: {str(e)}")],
                "engineering_status": "blocked",
                "errors": state.get("errors", []) + [f"Developer error: {str(e)}"],
            }

    async def _wait_for_scaffolding(
        self, project_spec: dict, action: str, state: dict
    ) -> dict | None:
        """Wait for scaffolding completion if needed.

        Returns:
            dict: Error state if failed/timeout.
            dict: Updated project_spec if successful.
            None: If waiting was not required or successful without update.
        """
        project_id = project_spec.get("id")
        project_status = project_spec.get("status", "draft")

        if project_id and action == "create" and project_status in ("draft", "scaffolding"):
            logger.info("waiting_for_scaffolding", project_id=project_id)
            for attempt in range(30):  # 30 * 10s = 5 min
                project = await api_client.get_project(project_id)
                if project:
                    status = project.get("status")
                    if status == "scaffolded":
                        logger.info("scaffolding_complete", project_id=project_id)
                        return project  # Return updated spec
                    if status == "scaffold_failed":
                        logger.error("scaffolding_failed", project_id=project_id)
                        return {
                            "messages": [AIMessage(content="❌ Project scaffolding failed.")],
                            "engineering_status": "blocked",
                            "errors": state.get("errors", []) + ["Scaffolding failed"],
                        }
                if attempt > 0 and attempt % 6 == 0:
                    logger.info(
                        "waiting_for_scaffolding_progress", project_id=project_id, attempt=attempt
                    )
                await asyncio.sleep(10)
            else:
                logger.error("scaffolding_timeout", project_id=project_id)
                return {
                    "messages": [AIMessage(content="❌ Scaffolding timeout (5 min).")],
                    "engineering_status": "blocked",
                    "errors": state.get("errors", []) + ["Scaffolding timeout"],
                }

        elif action != "create":
            logger.info(
                "skipping_scaffolding_wait",
                project_id=project_id,
                action=action,
                project_status=project_status,
            )
            # Refresh project data for existing projects
            if project_id:
                fresh_project = await api_client.get_project(project_id)
                if fresh_project:
                    return fresh_project

        return None

    def _determine_repository(self, project_spec: dict, project_name: str) -> dict:
        """Determine repository details (owner, name, full_name)."""
        repository_url = project_spec.get("repository_url")
        if repository_url and "github.com/" in repository_url:
            repo_full_name = repository_url.split("github.com/")[-1].rstrip("/")
            owner, repo_name = repo_full_name.split("/", 1)
            logger.info(
                "using_repository_url_from_project",
                repository_url=repository_url,
                repo_full_name=repo_full_name,
            )
        else:
            owner = os.getenv("GITHUB_ORG")
            if not owner:
                raise RuntimeError("No repository_url in project and GITHUB_ORG env not set")
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
        )

    def _build_create_task(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        project_spec: dict,
    ) -> str:
        """Build task message for new project creation (scaffolded)."""
        modules_str = ",".join(modules)

        return f"""# Task: Build {project_name}

## Project Specification

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}

**Detailed Spec**:
{project_spec.get("detailed_spec", "N/A")}

## Project Structure (already scaffolded)

The project was scaffolded with `copier` from `service-template`.
You'll find:
- `services/{modules_str.split(",")[0]}/` - main service directory
- `shared/spec/models.yaml` - domain models definition
- `shared/spec/events.yaml` - events definition
- `/home/worker/TASK.md` - detailed requirements
- `AGENTS.md` - code structure patterns
- `Makefile` - build commands

Run `make generate` after modifying spec files to regenerate code.

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

        return f"""# Task: {action_label} in {project_name}

## What To Do

{task_description}

## Project Context

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}

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
