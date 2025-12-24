"""Zavhoz (Resource Manager) agent node.

Zavhoz manages infrastructure by reading from internal DB.
All external API calls (Time4VPS) are done by background sync workers,
not by the agent directly. This saves tokens and keeps secrets isolated.
"""

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

# Import tools - only DB tools, no direct Time4VPS access
from ..tools.database import (
    list_managed_servers,
    find_suitable_server,
    get_server_info,
    allocate_port,
    get_next_available_port,
)

# Initialize Tools
tools = [
    list_managed_servers,
    find_suitable_server,
    get_server_info,
    allocate_port,
    get_next_available_port,
]

# Initialize LLM with tools
llm = ChatOpenAI(model="gpt-4o", temperature=0)
llm_with_tools = llm.bind_tools(tools)


SYSTEM_PROMPT = """You are Zavhoz, the infrastructure manager for the codegen orchestrator.

Your responsibilities:
1. Find suitable servers for projects based on resource requirements (RAM, disk)
2. Allocate ports to avoid collisions between services
3. Report server status and capacity

You have access to the internal database which is synced with Time4VPS API.
All server data (capacity, usage) is up-to-date in the database.

Available tools:
- list_managed_servers(): Get all active managed servers with their capacity
- find_suitable_server(min_ram_mb, min_disk_mb): Find a server with enough resources
- get_server_info(handle): Get details about a specific server
- allocate_port(server_handle, port, service_name, project_id): Reserve a port
- get_next_available_port(server_handle, start_port): Find next free port

When asked to provision resources:
1. Use find_suitable_server to find a server matching requirements
2. If found, allocate the needed port using get_next_available_port + allocate_port
3. Return the server handle, IP, and allocated port

Be concise in your responses. Return structured data when possible.
"""


def run(state: dict) -> dict:
    """Run zavhoz agent.

    Reads infrastructure data from DB and allocates resources.
    """
    messages = state.get("messages", [])
    project_spec = state.get("project_spec", {})
    
    # Add system message if this is first invocation
    if not any(msg.type == "system" for msg in messages if hasattr(msg, 'type')):
        system_msg = SystemMessage(content=SYSTEM_PROMPT + f"\n\nCurrent Project Spec: {project_spec}")
        messages = [system_msg] + list(messages)

    # Invoke LLM
    response = llm_with_tools.invoke(messages)
    
    return {
        "messages": [response],
        "current_agent": "zavhoz",
    }
