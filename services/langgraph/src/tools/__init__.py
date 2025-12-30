"""Tools for LangGraph agents.

Re-exports all tools for backward compatibility.
Import from here instead of individual modules.
"""

# Project management
# Project activation flow
from .activation import (
    activate_project,
    check_ready_to_deploy,
    inspect_repository,
    save_project_secret,
)

# Phase 4: Admin capability
from .admin import clear_project_state, list_graph_nodes, trigger_node_manually

# Architect tools (module selection, deployment hints)
from .architect_tools import (
    AVAILABLE_MODULES,
    customize_task_instructions,
    select_modules,
    set_deployment_hints,
    set_project_complexity,
)

# Delegation tools
from .delegation import delegate_to_analyst

# Deploy capability (Phase 4)
from .deploy import (
    check_deploy_readiness,
    get_deploy_logs,
    get_deploy_status,
    trigger_deploy,
)

# Phase 4: Diagnose capability
from .diagnose import check_service_health, get_error_history, get_service_logs

# Phase 4: Engineering capability
from .engineering import get_engineering_status, trigger_engineering, view_latest_pr

# GitHub tools
from .github import create_file_in_repo, create_github_repo, get_github_token

# Incident tracking
from .incidents import create_incident, list_active_incidents

# Phase 4: Infrastructure capability
from .infrastructure import list_allocations, release_port

# Port allocation
from .ports import allocate_port, get_next_available_port
from .projects import (
    create_project,
    create_project_intent,
    get_project_status,
    list_projects,
    set_project_maintenance,
    update_project,
)

# RAG search tools
from .rag import search_project_context, search_user_context

# Resource inventory
from .resources import create_service_deployment, list_resource_inventory

# Server management
from .servers import (
    find_suitable_server,
    get_server_info,
    get_services_on_server,
    list_managed_servers,
    update_server_status,
)

# Spec tools
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
    # Incidents
    "create_incident",
    "list_active_incidents",
    # Activation
    "activate_project",
    "inspect_repository",
    "save_project_secret",
    "check_ready_to_deploy",
    # Resources
    "list_resource_inventory",
    "create_service_deployment",
    # GitHub
    "create_github_repo",
    "get_github_token",
    # Delegation
    "delegate_to_analyst",
    "create_file_in_repo",
    # Specs
    "get_project_spec",
    "update_project_spec",
    "create_project_spec_yaml",
    "create_spec_md",
    # RAG
    "search_project_context",
    "search_user_context",
    # Architect tools
    "AVAILABLE_MODULES",
    "select_modules",
    "set_deployment_hints",
    "customize_task_instructions",
    "set_project_complexity",
    # Deploy (Phase 4)
    "trigger_deploy",
    "get_deploy_status",
    "get_deploy_logs",
    "check_deploy_readiness",
    # Infrastructure (Phase 4)
    "list_allocations",
    "release_port",
    # Engineering (Phase 4)
    "trigger_engineering",
    "get_engineering_status",
    "view_latest_pr",
    # Diagnose (Phase 4)
    "get_service_logs",
    "check_service_health",
    "get_error_history",
    # Admin (Phase 4)
    "list_graph_nodes",
    "trigger_node_manually",
    "clear_project_state",
]
