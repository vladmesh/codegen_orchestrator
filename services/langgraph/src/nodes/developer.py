"""Unified Developer node.

Handles architecture, scaffolding with copier, and coding in a single execution.
Spawns a Claude Code worker with copier capability and sends comprehensive task.
"""

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.worker_spawner import request_spawn
from ..config.constants import Timeouts
from .base import FunctionalNode

logger = structlog.get_logger()

# Max error message length for Telegram display
MAX_ERROR_MSG_LENGTH = 500


class DeveloperNode(FunctionalNode):
    """Unified Developer node - handles architecture, scaffolding, and coding.

    Spawns a Claude Code worker with copier capability to:
    1. Create/clone GitHub repository
    2. Run copier to scaffold project structure
    3. Implement business logic
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
        description = project_spec.get("description", "")
        modules = project_spec.get("modules", ["backend"])

        logger.info(
            "developer_node_start",
            project_name=project_name,
            modules=modules,
        )

        try:
            # Get GitHub App token for the project's organization
            github_client = GitHubAppClient()

            # Auto-detect org from GitHub App installation
            installation = await github_client.get_first_org_installation()
            owner = installation["org"]

            repo_name = project_name.lower().replace(" ", "-").replace("_", "-")
            repo_full_name = f"{owner}/{repo_name}"

            # Get token for the repository
            access_token = await github_client.get_token(owner, repo_name)

            # Build comprehensive task message for Claude
            task_message = self._build_task_message(
                project_name=project_name,
                description=description,
                modules=modules,
                repo_full_name=repo_full_name,
                project_spec=project_spec,
            )

            # Spawn worker with copier capability
            worker_result = await request_spawn(
                repo=repo_full_name,
                github_token=access_token,
                task_content=task_message,
                task_title=f"Build {project_name}",
                timeout_seconds=Timeouts.WORKER_SPAWN,
            )

            if worker_result.success:
                logger.info(
                    "developer_node_success",
                    project_name=project_name,
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

    def _build_task_message(
        self,
        project_name: str,
        description: str,
        modules: list[str],
        repo_full_name: str,
        project_spec: dict,
    ) -> str:
        """Build comprehensive task message for Claude.

        This message instructs Claude to:
        - Create GitHub repo if needed
        - Clone the repository
        - Run copier with service-template
        - Implement business logic
        - Commit and push
        """
        modules_str = ",".join(modules)

        task = f"""# Task: Build {project_name}

## Project Specification

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}

**Detailed Spec**:
{project_spec.get("detailed_spec", "N/A")}

## Implementation Steps

### 1. Setup Repository

Repository {repo_full_name} is already created. Clone to your workspace:
```bash
git clone https://github.com/{repo_full_name}
cd {repo_full_name.split("/")[-1]}
```

### 2. Scaffold Project Structure

Use copier to create project structure from service-template:

```bash
copier copy gh:vladmesh/service-template . \\
  --data project_name={project_name} \\
  --data modules={modules_str} \\
  --trust \\
  --defaults
```

This will create the basic structure with selected modules ({modules_str}).

### 3. Write Business Logic

Implement the business logic according to the specification:
- Read the generated TASK.md for detailed requirements
- Follow patterns in AGENTS.md for code structure
- Implement all required functionality

### 4. Commit and Push

After implementation:
```bash
git add .
git commit -m \"feat: implement {project_name}\"
git push origin main
```

## Expected Output

After completing all steps, provide a summary including:
- Commit SHA
- What was implemented
- Any important notes or next steps

## Important Notes

- Use copier version 9.4.1 (should be pre-installed)
- Follow the project structure conventions from service-template
- Ensure all code is properly formatted and tested
- Make descriptive commit messages
"""
        return task


# Export singleton instance
developer_node = DeveloperNode()
