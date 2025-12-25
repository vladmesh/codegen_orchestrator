"""Zavhoz (Resource Manager) agent node.

Zavhoz manages infrastructure by reading from internal DB.
All external API calls (Time4VPS) are done by background sync workers,
not by the agent directly. This saves tokens and keeps secrets isolated.
"""

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

# Import tools - only DB tools, no direct Time4VPS access
from ..tools.database import (
    allocate_port,
    find_suitable_server,
    get_next_available_port,
    get_project_status,
    get_server_info,
    list_managed_servers,
)

# Initialize Tools
tools = [
    list_managed_servers,
    find_suitable_server,
    get_server_info,
    allocate_port,
    get_next_available_port,
    get_project_status,
]

# Tool name to function mapping
tools_map = {tool.name: tool for tool in tools}

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
- get_project_status(project_id): Get project details (including config/requirements)

**CRITICAL: For DEPLOY flows, you MUST allocate resources!**

When the intent is 'deploy' or you're asked to provision resources:
1. Use `find_suitable_server(128, 512)` to find a server (use defaults: 128MB RAM, 512MB disk for simple bots).
2. Use `get_next_available_port(server_handle, 8000)` to find a free port.
3. Use `allocate_port(server_handle, port, project_id, project_id)` to reserve it.
4. Confirm the allocation with server handle, IP, and port.

Do NOT just respond with text - you MUST call tools to allocate resources!

Be concise in your responses. Return structured data when possible.
"""


async def run(state: dict) -> dict:
    """Run zavhoz agent.

    Reads infrastructure data from DB and allocates resources.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Starting Zavhoz agent run...")

    messages = state.get("messages", [])
    project_spec = state.get("project_spec", {})
    po_intent = state.get("po_intent", "")
    current_project = state.get("current_project", "")

    # Build context for the agent
    context_parts = []
    if project_spec:
        context_parts.append(f"Project Spec: {project_spec}")
    if current_project:
        context_parts.append(f"Project ID: {current_project}")
    if po_intent:
        context_parts.append(f"Intent: {po_intent}")
        if po_intent == "deploy":
            context_parts.append("ACTION REQUIRED: Allocate server resources for deployment!")
    
    context = "\n".join(context_parts) if context_parts else "No project context."

    # Add system message if this is first invocation
    if not any(msg.type == "system" for msg in messages if hasattr(msg, "type")):
        system_msg = SystemMessage(
            content=SYSTEM_PROMPT + f"\n\n--- Current Context ---\n{context}"
        )
        messages = [system_msg] + list(messages)

    # Invoke LLM
    response = await llm_with_tools.ainvoke(messages)

    return {
        "messages": [response],
        "current_agent": "zavhoz",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Zavhoz LLM.

    Processes server/port allocation tools and updates state.
    """
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    tool_results = []
    allocated_resources = state.get("allocated_resources", {}).copy()

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_func = tools_map.get(tool_name)

        if tool_func:
            try:
                result = await tool_func.ainvoke(tool_call["args"])

                # Track allocated resources
                if tool_name == "allocate_port" and result:
                    port_key = f"{result.get('server_handle')}:{result.get('port')}"
                    allocated_resources[port_key] = result

                tool_results.append(
                    ToolMessage(
                        content=f"Result: {result}",
                        tool_call_id=tool_call["id"],
                    )
                )
            except Exception as e:
                tool_results.append(
                    ToolMessage(
                        content=f"Error: {e!s}",
                        tool_call_id=tool_call["id"],
                    )
                )
        else:
            tool_results.append(
                ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            )

    return {
        "messages": tool_results,
        "allocated_resources": allocated_resources,
    }
