"""Zavhoz (Resource Manager) agent node."""

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage, SystemMessage

# Import tools
from ..tools.time4vps import list_servers, get_server_details, reinstall_server, list_dns_zones
from ..tools.database import register_server_in_db, allocate_port

# Initialize Tools
tools = [
    list_servers,
    get_server_details,
    reinstall_server,
    list_dns_zones,
    register_server_in_db,
    allocate_port
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
