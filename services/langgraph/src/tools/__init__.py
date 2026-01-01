"""Tools for LangGraph subgraphs.

After CLI Agent migration (Phase 8), only tools used by:
- Engineering subgraph (Architect, Preparer, Developer)
- DevOps subgraph (Deployer)
- Analyst node
- Zavhoz node (resource allocation)

PO capability tools are replaced by orchestrator-cli commands.
"""

# Architect tools (module selection, deployment hints)
from .architect_tools import (
    AVAILABLE_MODULES,
    customize_task_instructions,
    select_modules,
    set_deployment_hints,
    set_project_complexity,
)

# DevOps tools (deployment)
from .devops_tools import run_ansible_deploy

# GitHub tools
from .github import create_file_in_repo, create_github_repo, get_github_token

# Port allocation (used by Zavhoz)
from .ports import allocate_port, get_next_available_port

# Project tools (used by Analyst and subgraphs)
from .projects import (
    create_project,
    create_project_intent,
    get_project_status,
    list_projects,
    set_project_maintenance,
    update_project,
)

# Server management (used by Zavhoz)
from .servers import (
    find_suitable_server,
    get_server_info,
    get_services_on_server,
    list_managed_servers,
    update_server_status,
)

# Spec tools (used by Analyst)
from .specs import create_project_spec_yaml, create_spec_md, get_project_spec, update_project_spec

__all__ = [
    # Projects
    "create_project",
    "list_projects",
    "get_project_status",
    "create_project_intent",
    "set_project_maintenance",
    "update_project",
    # Servers
    "list_managed_servers",
    "find_suitable_server",
    "get_server_info",
    "update_server_status",
    "get_services_on_server",
    # Ports
    "allocate_port",
    "get_next_available_port",
    # GitHub
    "create_github_repo",
    "get_github_token",
    "create_file_in_repo",
    # Specs
    "get_project_spec",
    "update_project_spec",
    "create_project_spec_yaml",
    "create_spec_md",
    # Architect tools
    "AVAILABLE_MODULES",
    "select_modules",
    "set_deployment_hints",
    "customize_task_instructions",
    "set_project_complexity",
    # DevOps tools
    "run_ansible_deploy",
]
