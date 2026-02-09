"""Tool groups for orchestrator CLI.

This module defines the available tool groups and their documentation.
Used by workers-spawner to generate AGENTS.md/CLAUDE.md files.
"""

from enum import Enum
from pathlib import Path

import yaml


class ToolGroup(str, Enum):
    """Available orchestrator CLI tool groups.

    Each group corresponds to a subcommand in orchestrator-cli.
    """

    PROJECT = "project"
    DEPLOY = "deploy"
    ENGINEERING = "engineering"
    INFRA = "infra"
    DIAGNOSE = "diagnose"
    RESPOND = "respond"  # Special: respond_to_user capability


# Documentation for each tool group
# Used to generate agent instruction files
TOOL_DOCS: dict[ToolGroup, str] = {
    ToolGroup.PROJECT: """## Project Commands

Manage projects in the orchestrator system.

```bash
# Create a new project with modules and description
orchestrator project create --name <name> --modules <modules> \\
    --description "<what to build>"

# Available modules (comma-separated):
#   backend    - FastAPI REST API (always included)
#   tg_bot     - Telegram bot service
#   notifications - Notification worker
#   frontend   - Frontend application

# Examples:
orchestrator project create --name my-api --modules backend \\
    --description "REST API for user management"

orchestrator project create --name my-bot --modules backend,tg_bot \\
    --description "Telegram bot that reverses words in messages"

# List all projects
orchestrator project list

# Get project details by ID
orchestrator project get <project_id>

# Set a secret for a project (e.g., API tokens)
orchestrator project set-secret -p <project_id> -k <KEY> -v <value>
```

**Important**:
- Always create a project before triggering engineering or deploy tasks.
- For Telegram bots, use `--modules backend,tg_bot` (NOT just "telegram").
- ALWAYS include `--description` with what the project should do!
""",
    ToolGroup.DEPLOY: """## Deploy Commands

Deploy existing projects WITHOUT code changes. Use this when:
- Redeploying after a failed deployment
- Deploying existing code to a server
- User explicitly asks to "deploy" or "redeploy"

```bash
# Trigger deployment for a project (NO code changes)
orchestrator deploy trigger -p <project_id>

# Check deployment status
orchestrator deploy status <task_id>
```

**Note**: If you need to CREATE or MODIFY code, use `engineering trigger` instead.
""",
    ToolGroup.ENGINEERING: """## Engineering Commands

Run FULL development flow: code generation + testing + deployment. Use this when:
- Creating a new project from scratch
- Modifying existing code/adding features
- User wants to "build", "create", or "change" something

```bash
# Trigger FULL engineering task (code + deploy)
orchestrator engineering trigger -p <project_id>

# Check task status (with optional --follow for live updates)
orchestrator engineering status <task_id> [--follow]
```

**Note**: If code already exists and you just need to deploy, use `deploy trigger` instead.
""",
    ToolGroup.INFRA: """## Infrastructure Commands

Manage infrastructure resources.

```bash
# List available servers
orchestrator infra servers

# Get server details
orchestrator infra server <server_handle>
```
""",
    ToolGroup.DIAGNOSE: """## Diagnose Commands

Debug and diagnose system issues.

```bash
# Run diagnostics
orchestrator diagnose run
```
""",
    ToolGroup.RESPOND: """## Respond to User

When you have completed your task or need user input, use this to communicate:

```bash
# Send final response to user
orchestrator respond "<message>"

# Ask clarifying question (expects user reply)
orchestrator respond "<question>" --expect-reply
```

**Important**: Always use this command to report results or ask questions.
""",
}


# Path to prompts directory (relative to this file)
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_po_prompt() -> dict:
    """Load PO agent prompt from YAML file.

    Returns:
        Dict with role, scenarios, rules sections.
    """
    prompt_file = PROMPTS_DIR / "po_agent.yml"
    if not prompt_file.exists():
        return {}

    with open(prompt_file) as f:
        return yaml.safe_load(f) or {}


def get_instructions_content(allowed_tools: list[ToolGroup]) -> str:
    """Generate instruction file content for given tool groups.

    Args:
        allowed_tools: List of tool groups the agent is allowed to use.

    Returns:
        Markdown content with documentation for allowed tools.
    """
    from .tool_registry import get_registered_commands, load_cli_commands

    # Import CLI commands to trigger @register_tool decorators
    load_cli_commands()

    sections: list[str] = []

    # Load and add PO system prompt
    po_prompt = _load_po_prompt()
    if po_prompt:
        if role := po_prompt.get("role"):
            sections.append(role)
            sections.append("")

        if scenarios := po_prompt.get("scenarios"):
            for scenario_content in scenarios.values():
                sections.append(scenario_content)
                sections.append("")

        if rules := po_prompt.get("rules"):
            sections.append(rules)
            sections.append("")

    # Add CLI documentation
    sections.append("# Orchestrator CLI Reference")
    sections.append("")
    sections.append(
        "You have access to the `orchestrator` CLI tool. "
        "Use it to interact with the orchestrator system."
    )
    sections.append("")

    # Generate documentation from registered commands
    for tool in allowed_tools:
        commands = get_registered_commands(tool)
        if not commands:
            # Fallback to hardcoded docs if no commands registered
            if tool in TOOL_DOCS:
                sections.append(TOOL_DOCS[tool])
            continue

        # Generate docs from registry
        sections.append(f"## {tool.value.title()} Commands")
        sections.append("")
        sections.append("```bash")
        for cmd in commands:
            sections.append(f"# {cmd['description']}")
            sections.append(f"orchestrator {tool.value} {cmd['name']} ...")
            sections.append("")
        sections.append("```")
        sections.append("")

    sections.append("---")
    sections.append("")
    sections.append("**Tip**: Use `orchestrator --help` to see all available commands.")
    sections.append("")

    return "\n".join(sections)
