"""Capability Registry for Dynamic ProductOwner.

Groups tools by capability for dynamic loading.
Intent parser selects capabilities, PO gets only relevant tools.
"""

from typing import Any

from langchain_core.tools import BaseTool

# Import existing tools that will be mapped to capabilities
from ..tools import (
    # Activation / Deploy
    activate_project,
    # Ports
    allocate_port,
    check_ready_to_deploy,
    # Phase 4: Diagnose capability
    check_service_health,
    # Phase 4: Admin capability
    clear_project_state,
    create_project_intent,
    # Delegation
    delegate_to_analyst,
    find_suitable_server,
    # Phase 4: Deploy capability
    get_deploy_logs,
    get_deploy_status,
    # Phase 4: Engineering capability
    get_engineering_status,
    get_error_history,
    get_next_available_port,
    get_project_status,
    get_server_info,
    get_service_logs,
    inspect_repository,
    # Incidents
    list_active_incidents,
    # Phase 4: Infrastructure capability
    list_allocations,
    list_graph_nodes,
    # Servers
    list_managed_servers,
    # Project management
    list_projects,
    # Resources
    list_resource_inventory,
    release_port,
    save_project_secret,
    # RAG
    search_project_context,
    set_project_maintenance,
    trigger_deploy,
    trigger_engineering,
    trigger_node_manually,
    update_project,
    view_latest_pr,
)
from .base import BASE_TOOLS

# Capability Registry: groups of related tools
CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "deploy": {
        "description": "Deploy projects to servers",
        "tools": [
            "check_ready_to_deploy",
            "activate_project",
            "inspect_repository",
            "save_project_secret",
            "trigger_deploy",
            "get_deploy_status",
            "get_deploy_logs",
        ],
    },
    "infrastructure": {
        "description": "Manage servers and resource allocation",
        "tools": [
            "list_managed_servers",
            "find_suitable_server",
            "get_server_info",
            "allocate_port",
            "get_next_available_port",
            "list_resource_inventory",
            "list_allocations",
            "release_port",
        ],
    },
    "project_management": {
        "description": "Create and manage projects",
        "tools": [
            "list_projects",
            "get_project_status",
            "create_project_intent",
            "set_project_maintenance",
            "update_project",
        ],
    },
    "engineering": {
        "description": "Trigger code implementation pipeline",
        "tools": [
            "delegate_to_analyst",
            "trigger_engineering",
            "get_engineering_status",
            "view_latest_pr",
        ],
    },
    "diagnose": {
        "description": "Debug issues, view logs, check health",
        "tools": [
            "list_active_incidents",
            "search_project_context",
            "get_service_logs",
            "check_service_health",
            "get_error_history",
        ],
    },
    "admin": {
        "description": "System administration and manual control",
        "tools": [
            "list_graph_nodes",
            "trigger_node_manually",
            "clear_project_state",
        ],
    },
}

# Tool name → Tool function mapping
TOOLS_MAP: dict[str, BaseTool] = {
    # Project management
    "list_projects": list_projects,
    "get_project_status": get_project_status,
    "create_project_intent": create_project_intent,
    "set_project_maintenance": set_project_maintenance,
    "update_project": update_project,
    # Deploy / Activation
    "check_ready_to_deploy": check_ready_to_deploy,
    "activate_project": activate_project,
    "inspect_repository": inspect_repository,
    "save_project_secret": save_project_secret,
    # Servers
    "list_managed_servers": list_managed_servers,
    "find_suitable_server": find_suitable_server,
    "get_server_info": get_server_info,
    # Ports
    "allocate_port": allocate_port,
    "get_next_available_port": get_next_available_port,
    # Resources
    "list_resource_inventory": list_resource_inventory,
    # RAG
    "search_project_context": search_project_context,
    # Delegation
    "delegate_to_analyst": delegate_to_analyst,
    # Incidents
    "list_active_incidents": list_active_incidents,
    # Phase 4: Deploy
    "trigger_deploy": trigger_deploy,
    "get_deploy_status": get_deploy_status,
    "get_deploy_logs": get_deploy_logs,
    # Phase 4: Infrastructure
    "list_allocations": list_allocations,
    "release_port": release_port,
    # Phase 4: Engineering
    "trigger_engineering": trigger_engineering,
    "get_engineering_status": get_engineering_status,
    "view_latest_pr": view_latest_pr,
    # Phase 4: Diagnose
    "get_service_logs": get_service_logs,
    "check_service_health": check_service_health,
    "get_error_history": get_error_history,
    # Phase 4: Admin
    "list_graph_nodes": list_graph_nodes,
    "trigger_node_manually": trigger_node_manually,
    "clear_project_state": clear_project_state,
}


def get_tools_for_capabilities(capabilities: list[str]) -> list[BaseTool]:
    """Build tool list from capability names.

    Always includes BASE_TOOLS (respond_to_user, search_knowledge, etc).

    Args:
        capabilities: List of capability names to include

    Returns:
        List of BaseTool objects for the given capabilities
    """
    tools = list(BASE_TOOLS)
    seen_names = {t.name for t in tools}

    for cap_name in capabilities:
        cap = CAPABILITY_REGISTRY.get(cap_name)
        if not cap:
            continue
        for tool_name in cap["tools"]:
            if tool_name in seen_names:
                continue
            tool = TOOLS_MAP.get(tool_name)
            if tool:
                tools.append(tool)
                seen_names.add(tool_name)

    return tools


def list_available_capabilities() -> dict[str, str]:
    """Return capability descriptions for intent parser prompt.

    Returns:
        Dict mapping capability name to description
    """
    return {name: cap["description"] for name, cap in CAPABILITY_REGISTRY.items()}


__all__ = [
    "CAPABILITY_REGISTRY",
    "TOOLS_MAP",
    "BASE_TOOLS",
    "get_tools_for_capabilities",
    "list_available_capabilities",
]
