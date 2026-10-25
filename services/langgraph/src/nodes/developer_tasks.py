"""Task message builders and repo helpers for the Developer node.

Builds TASK.md content sent to developer workers. Extracted from
developer.py to keep each module under 400 LOC.
"""

import os

import structlog

from shared.contracts.dto.local_install import LocalPackageBuild

logger = structlog.get_logger()


def determine_repository(git_url: str | None, project_name: str, project_slug: str | None) -> dict:
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
        if not project_slug:
            raise RuntimeError("No repository found for project and project slug is missing")
        repo_name = project_slug
        repo_full_name = f"{owner}/{repo_name}"
        logger.info(
            "inferring_repo_from_project_slug",
            project_slug=project_slug,
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


def format_acceptance_criteria(
    task_acceptance_criteria: str | None, repository_acceptance_criteria: str | None
) -> str:
    """Render what QA checks, verbatim. Empty string when there are no criteria."""
    sections = []
    if task_acceptance_criteria and task_acceptance_criteria.strip():
        sections.append(f"""
## Acceptance Criteria (QA checks these literally)

QA judges this task by the criteria below, word for word. Every quoted reply,
message text and value must match exactly, including punctuation and quote
marks. Do not rephrase them.

{task_acceptance_criteria.strip()}
""")
    if repository_acceptance_criteria and repository_acceptance_criteria.strip():
        sections.append(f"""
## Post-Deploy QA Checklist (central QA runs all of it after deploy)

After deploy, central QA checks the running application against every line
below, literally. Your change must keep all of them passing; quoted replies must
match exactly.

{repository_acceptance_criteria.strip()}
""")
    return "".join(sections)


def format_package_build(build: LocalPackageBuild | None) -> str:
    """Render the task's stored package build intent. Empty string for ordinary work."""
    if build is None:
        return ""
    package = build.package
    return f"""
## Product-Local Package Source

This task writes the source of one new kit package that belongs to this product only.
The platform stored this intent with the task. It is the expected output, not proof that
the package exists or is compatible: after your commit is published, the platform installs
exactly that commit in a separate deterministic step, which checks the real package.

- Source directory: `{build.path}` (product-relative, the only directory the package lives in)
- Package name: `{package.name}`; distribution: `{package.distribution}`;
  version: `{package.version}`
- Expected kit core `{build.core_version}`, product Python `{build.python_version}` and kit
  tooling commit `{build.tooling_commit}`: the platform's expectations. Read the product's
  own `codegen_kit` core and tooling, and report a mismatch instead of working around it.
- The rest of this TASK.md, the product's AGENTS.md and the story context are the
  product the package serves; it reaches that product only through its declared
  extension points.

Write it as an installable package protocol v1 source, following the package-authoring
contract the product itself carries (its `codegen_kit` package runtime and the kit's
"Package protocol v1"); do not invent another manifest format:

- `{build.path}/pyproject.toml` declares a static `[project]` name `{package.distribution}`,
  version `{package.version}`, a `requires-python` admitting `{build.python_version}`, and
  exactly one `codegen_kit.packages` entry point named `{package.name}`.
- The entry-point module is a package directory holding the one `package.yaml`:
  `protocol_version: 1`, name `{package.name}`, version `{package.version}`, a
  `requires_core` admitting `{build.core_version}`, and the extension points the
  capability uses (`http.prefix`, `events`, `settings_schema`, `jobs_schema`, `database`).
- Parameterize behaviour through the package's own `settings_schema`; hard-code no
  product values, user IDs or credentials.
- Ship the package's own behavioural tests inside `{build.path}` and make them pass;
  commit and push the package through the normal engineering lifecycle.
- Commit every source file as a tracked regular file. When you push, `{build.path}` holds
  no untracked, ignored or cache entries (`__pycache__`, `.pytest_cache`, `dist/`,
  `build/`, `*.egg-info`, virtual environments).
- Keep the source outside `services/backend/packages/`, which holds installed wheels only.
- Do not install it: no `kit add`, `kit bind`, `uv add` or `pip install` of it, no wheel
  in `services/backend/packages/`, no entry in `services/backend/manifest.yaml` `packages`
  and no regeneration for it. Where the product's tooling provides it, the read-only
  `kit check-install {package.name} --package-source {build.path} --json` previews the source.
- Do not copy a released kit package's implementation, and publish no catalog entry,
  tag or release.
"""


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


def format_generator_hint(modules: list[str]) -> str:
    """Name the documented generator for modules with repository-owned output."""
    if "backend" not in modules:
        return ""
    return "For backend manifest or spec changes, run `make generate-from-spec`."


def build_create_task(
    project_name: str,
    description: str,
    modules: list[str],
    project_spec: dict,
    feature_description: str | None = None,
    story_context: str | None = None,
    task_acceptance_criteria: str | None = None,
    repository_acceptance_criteria: str | None = None,
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

    generator_hint = format_generator_hint(modules)
    generate_hint = f"\n{generator_hint}\n" if generator_hint else ""

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

The project was scaffolded with `copier` from `codegen-product-kit`.
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
{format_acceptance_criteria(task_acceptance_criteria, repository_acceptance_criteria)}\
{format_story_context(story_context)}"""


def build_feature_task(
    project_name: str,
    description: str,
    modules: list[str],
    action: str,
    feature_description: str | None,
    project_spec: dict,
    story_context: str | None = None,
    task_acceptance_criteria: str | None = None,
    repository_acceptance_criteria: str | None = None,
    package_build: LocalPackageBuild | None = None,
) -> str:
    """Build task message for feature addition or bug fix."""
    action_label = "Add Feature" if action == "feature" else "Fix Issue"
    task_description = feature_description or description or "No description provided"
    modules_str = ", ".join(modules)
    env_hints_section = format_env_hints(project_spec)
    generator_hint = format_generator_hint(modules)
    generator_requirement = f" {generator_hint}" if generator_hint else ""
    generator_rule = (
        "- When a manifest or spec change owns derived artifacts, "
        f"run its documented generator.{generator_requirement}"
    )
    return f"""# Task: {action_label} in {project_name}

## What To Do

{task_description}
{format_package_build(package_build)}\
{format_acceptance_criteria(task_acceptance_criteria, repository_acceptance_criteria)}
## Project Context

**Name**: {project_name}
**Description**: {description}
**Modules**: {modules_str}
{env_hints_section}
## Important

- This is an **existing project** — do NOT re-scaffold it or perform a wholesale restructuring
{generator_rule}
- Read existing code to understand the architecture before making changes
- Make **targeted changes** — don't rewrite existing working code
- Keep changes minimal and focused on the task description
- Ensure all existing tests still pass after your changes
- Add tests for new functionality where appropriate
- Commit with descriptive message (e.g., "feat: add /stats command" or "fix: handle empty input")
{format_story_context(story_context)}"""


def build_task_message(  # noqa: PLR0913 — one TASK.md's whole content, each part named
    project_name: str,
    description: str,
    modules: list[str],
    repo_full_name: str,
    project_spec: dict,
    action: str = "create",
    feature_description: str | None = None,
    story_context: str | None = None,
    task_acceptance_criteria: str | None = None,
    repository_acceptance_criteria: str | None = None,
    *,
    package_build: LocalPackageBuild | None = None,
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
            task_acceptance_criteria=task_acceptance_criteria,
            repository_acceptance_criteria=repository_acceptance_criteria,
            package_build=package_build,
        )

    return build_create_task(
        project_name=project_name,
        description=description,
        modules=modules,
        project_spec=project_spec,
        feature_description=feature_description,
        story_context=story_context,
        task_acceptance_criteria=task_acceptance_criteria,
        repository_acceptance_criteria=repository_acceptance_criteria,
    )
