"""Task message builders and repo helpers for the Developer node.

Builds TASK.md content sent to developer workers. Extracted from
developer.py to keep each module under 400 LOC.
"""

import os

import structlog

logger = structlog.get_logger()


def determine_repository(git_url: str | None, project_name: str) -> dict:
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


def get_task_title(action: str, project_name: str) -> str:
    """Get task title based on action."""
    if action == "feature":
        return f"Add feature to {project_name}"
    if action == "fix":
        return f"Fix issue in {project_name}"
    return f"Build {project_name}"


def format_story_context(story_context: str | None) -> str:
    """Format story context section for task message. Empty string if no context."""
    if not story_context:
        return ""
    return f"""
## Story Context

Other tasks in this story (do NOT redo completed work):

{story_context}"""


def format_env_hints(project_spec: dict) -> str:
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


def build_create_task(
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

    env_hints_section = format_env_hints(project_spec)

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
- `AGENTS.md` - code structure patterns
- `Makefile` - build commands
{generate_hint}
## Implementation

Implement the business logic according to the specification above.
- Follow patterns in AGENTS.md for code structure
- Implement all required functionality
- Use existing generated code as foundation
{format_story_context(story_context)}"""


def build_feature_task(
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
    env_hints_section = format_env_hints(project_spec)

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
{format_story_context(story_context)}"""


def build_task_message(
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
        return build_feature_task(
            project_name=project_name,
            description=description,
            modules=modules,
            action=action,
            feature_description=feature_description,
            project_spec=project_spec,
            story_context=story_context,
        )

    return build_create_task(
        project_name=project_name,
        description=description,
        modules=modules,
        project_spec=project_spec,
        feature_description=feature_description,
        story_context=story_context,
    )
