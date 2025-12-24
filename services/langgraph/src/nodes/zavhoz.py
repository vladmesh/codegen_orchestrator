"""Zavhoz (Resource Manager) agent node."""

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, SystemMessage

# Import tools
from ..tools.time4vps import (
    list_servers, 
    get_server_details, 
    reinstall_server,    
    get_dns_zones, 
    add_dns_record,
    order_server,
    get_available_os
)
from ..tools.database import (
    register_server_in_db, 
    allocate_port,
    list_managed_servers
)

# Initialize Tools
tools = [
    list_servers, # Keep for broad check if needed, or remove? User wants filtering.
    # Actually, list_servers (Time4VPS) should be used only for Ordering context?
    # Let's keep both but instruct via prompt which to use.
    # Or better: remove list_servers (Time4VPS) and force usage of list_managed_servers for "Inventory"
    # and only use order_server for "New".
    # But for now, let's just add the new tool.
    get_server_details, 
    order_server, 
    reinstall_server, 
    get_available_os,
    get_dns_zones,
    add_dns_record,
    register_server_in_db,
    allocate_port,
    list_managed_servers
]

# Initialize LLM with tools
llm = ChatOpenAI(model="gpt-5", temperature=0)
llm_with_tools = llm.bind_tools(tools)


def run(state: dict) -> dict:
    """Run zavhoz agent.

    Allocates resources for the project.
    Zavhoz manages infrastructure:
    1. Checks inventory (Time4VPS).
    2. ORDERS/PROVISIONS new servers if needed (via Time4VPS).
    3. Registers servers in internal DB.
    4. Allocates ports for services to avoid collision.
    """
    messages = state.get("messages", [])
    if not messages:
        # Initial instruction
        project_spec = state.get("project_spec", {})
        messages = [
            SystemMessage(content=f"""You are Zavhoz, the infrastructure manager.
Your goal is to prepare the infrastructure for project: {project_spec.get('name', 'unknown')}.

Responsibilities:
1. Ensure we have a server ready. Check Time4VPS inventory.
2. If no free server, order/reinstall one (Ask user for confirmation if ordering).
3. Register the server in our internal database.
4. Allocate ports for services defined in project_spec.

Current Project Spec: {project_spec}

Use your tools to inspect and provision resources.
If you need to make actions, call the appropriate tools.
""")
        ]

    # Invoke LLM
    response = llm_with_tools.invoke(messages)
    
    # Return updated state (append message)
    # LangGraph will handle tool execution if response has tool_calls
    return {
        "messages": [response],
        "current_agent": "zavhoz",
    }
