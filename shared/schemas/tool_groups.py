"""Tool groups for orchestrator CLI.

This module defines the available tool groups and their documentation.
Used by workers-spawner to generate AGENTS.md/CLAUDE.md files.
"""

from enum import Enum


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
# List all projects
orchestrator project list

# Get project details by ID
orchestrator project get <project_id>
```
""",
    ToolGroup.DEPLOY: """## Deploy Commands

Trigger and monitor deployments.

```bash
# Trigger deployment for a project
orchestrator deploy trigger <project_id>

# Check deployment status
orchestrator deploy status <job_id>
```
""",
    ToolGroup.ENGINEERING: """## Engineering Commands

Trigger engineering tasks (code generation, modifications).

```bash
# Trigger engineering task for a project
orchestrator engineering trigger <project_id>

# Check task status (with optional --follow for live updates)
orchestrator engineering status <task_id> [--follow]
```
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


def get_instructions_content(allowed_tools: list[ToolGroup]) -> str:
    """Generate instruction file content for given tool groups.

    Args:
        allowed_tools: List of tool groups the agent is allowed to use.

    Returns:
        Markdown content with documentation for allowed tools.
    """
    sections = [
        "# Orchestrator CLI",
        "",
        "You have access to the `orchestrator` CLI tool. "
        "Use it to interact with the orchestrator system.",
        "",
    ]

    for tool in allowed_tools:
        if tool in TOOL_DOCS:
            sections.append(TOOL_DOCS[tool])

    sections.append("---")
    sections.append("")
    sections.append("**Tip**: Use `orchestrator --help` to see all available commands.")
    sections.append("")

    return "\n".join(sections)
