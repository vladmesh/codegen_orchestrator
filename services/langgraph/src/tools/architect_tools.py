"""Architect Tools - project structure and module selection.

These tools are used by the Architect node to configure project structure
before the Preparer container runs copier with the selected modules.
"""

from typing import Annotated

from langchain_core.tools import tool

# Available modules from service-template
AVAILABLE_MODULES = ["backend", "tg_bot", "notifications", "frontend"]


@tool
def select_modules(
    modules: Annotated[
        list[str],
        "List of modules to include in the project. "
        "Available: backend, tg_bot, notifications, frontend",
    ],
) -> str:
    """Select which modules to include in the project.

    Available modules:
    - backend: FastAPI REST API with PostgreSQL database (most projects need this)
    - tg_bot: Telegram bot message handler (requires telegram token in secrets)
    - notifications: Background notifications processor (email, telegram)
    - frontend: Node.js frontend (port 4321)

    Choose based on project requirements. Most projects need at least 'backend'.
    If project involves Telegram bot, include 'tg_bot'.

    Returns a confirmation message or error if invalid modules specified.
    """
    if not modules:
        return (
            "Error: At least one module must be selected. "
            f"Available: {', '.join(AVAILABLE_MODULES)}"
        )

    invalid = [m for m in modules if m not in AVAILABLE_MODULES]
    if invalid:
        return f"Error: Invalid modules: {invalid}. Available: {AVAILABLE_MODULES}"

    return f"Selected modules: {modules}"


@tool
def set_deployment_hints(
    domain: Annotated[str | None, "Custom domain if needed (e.g., 'myapp.example.com')"] = None,
    backend_port: Annotated[int, "Port for backend service"] = 8000,
    frontend_port: Annotated[int, "Port for frontend if included"] = 4321,
    needs_ssl: Annotated[bool, "Whether to configure SSL certificate"] = True,
    environment_vars: Annotated[
        list[str] | None,
        "List of required environment variables (e.g., ['TELEGRAM_TOKEN', 'OPENAI_KEY'])",
    ] = None,
) -> str:
    """Set deployment configuration hints for DevOps.

    These hints are passed to the DevOps node for proper deployment configuration.
    Use this when the project has specific deployment requirements.

    Args:
        domain: Custom domain if needed (e.g., "myapp.example.com")
        backend_port: Port for backend service (default 8000)
        frontend_port: Port for frontend if included (default 4321)
        needs_ssl: Whether to configure SSL (default True)
        environment_vars: List of required env vars (e.g., ["TELEGRAM_TOKEN"])

    Returns confirmation of saved deployment hints.
    """
    hints = {
        "domain": domain,
        "backend_port": backend_port,
        "frontend_port": frontend_port,
        "needs_ssl": needs_ssl,
        "environment_vars": environment_vars or [],
    }
    return f"Deployment hints saved: {hints}"


@tool
def customize_task_instructions(
    instructions: Annotated[
        str,
        "Additional instructions for the developer. Will be appended to TASK.md.",
    ],
) -> str:
    """Add custom instructions for the developer.

    Use this to provide project-specific context that isn't in the spec.
    These instructions will be included in TASK.md that the developer reads.

    Examples:
    - "Use Redis for caching API responses"
    - "Integrate with OpenAI API for text generation"
    - "Follow the existing auth pattern from service X"

    Args:
        instructions: Custom instructions text to add to TASK.md

    Returns confirmation that instructions were saved.
    """
    if not instructions or not instructions.strip():
        return "Error: Instructions cannot be empty"

    return f"Custom instructions saved ({len(instructions)} chars)"


@tool
def set_project_complexity(
    complexity: Annotated[str, "Project complexity: 'simple' or 'complex'"],
) -> str:
    """Set the project complexity level.

    This determines how the engineering workflow proceeds:

    - simple: Basic project with straightforward business logic.
      Examples: CRUD service, echo bot, simple data processor.
      Developer implements everything in one pass.

    - complex: Project with non-trivial logic, multiple integrations,
      or complex workflows. Examples: e-commerce, orchestration system.
      Developer may need multiple iterations.

    Args:
        complexity: Either 'simple' or 'complex'

    Returns confirmation of complexity setting.
    """
    if complexity not in ("simple", "complex"):
        return f"Error: complexity must be 'simple' or 'complex', got '{complexity}'"

    return f"Project complexity set to: {complexity}"
