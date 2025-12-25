"""Product Owner (PO) agent node.

Classifies user intent and coordinates the high-level flow.
"""

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ..tools.database import (
    create_project_intent,
    get_project_status,
    list_active_incidents,
    list_projects,
    set_project_maintenance,
)

SYSTEM_PROMPT = """You are the Product Owner (PO) for the codegen orchestrator.

Your job:
1. **FIRST: Check for active incidents** by calling `list_active_incidents`.
   - If there are active incidents, alert the user immediately with 🚨 severity.
   - Explain which servers/services are affected and recovery status.
2. Classify user intent: new project / status request / update.
3. For a NEW project, call `create_project_intent` with intent="new_project".
   - Provide a short summary of the request.
   - Do NOT ask detailed requirements (Brainstorm handles that).
4. For STATUS requests, use:
   - `get_project_status` if user mentions a specific project ID.
   - Otherwise `list_projects`.
5. For UPDATE/MAINTENANCE requests:
   - Get the project ID from the user if missing.
   - Call `set_project_maintenance` with the project ID and update description.
   - This triggers the Engineering workflow (Architect → Developer → Tester).

Guidelines:
- Respond in the SAME LANGUAGE as the user.
- Do not invent project status; use tools.
- Keep responses concise.
- ALWAYS check incidents first before processing user request.
"""

# LLM with tools
llm = ChatOpenAI(model="gpt-4o", temperature=0.2)
tools = [
    list_active_incidents,  # Check incidents first
    list_projects,
    get_project_status,
    create_project_intent,
    set_project_maintenance,
]
llm_with_tools = llm.bind_tools(tools)

tools_map = {tool.name: tool for tool in tools}


def _format_project_line(project: dict) -> str:
    project_id = project.get("id", "unknown")
    name = project.get("name", "unknown")
    status = project.get("status", "unknown")
    description = ""
    config = project.get("config", {}) or {}
    if config.get("description"):
        description = f" - {config['description']}"
    return f"- {project_id} ({name}) [{status}]{description}"


async def run(state: dict) -> dict:
    """Run Product Owner agent."""
    messages = state.get("messages", [])

    llm_messages = [SystemMessage(content=SYSTEM_PROMPT)]
    llm_messages.extend(messages)

    response = await llm_with_tools.ainvoke(llm_messages)
    return {
        "messages": [response],
        "current_agent": "product_owner",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Product Owner LLM."""
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    tool_results = []
    response_parts = []
    po_intent = state.get("po_intent")
    project_intent = state.get("project_intent")

    for tool_call in last_message.tool_calls:
        tool_name = tool_call["name"]
        tool_func = tools_map.get(tool_name)

        if not tool_func:
            tool_results.append(
                ToolMessage(
                    content=f"Unknown tool: {tool_name}",
                    tool_call_id=tool_call["id"],
                )
            )
            continue

        result = await tool_func.ainvoke(tool_call["args"])
        tool_results.append(
            ToolMessage(
                content=f"Result: {result}",
                tool_call_id=tool_call["id"],
            )
        )

        if tool_name == "list_active_incidents":
            incidents = result or []
            if incidents:
                lines = ["🚨 **Активные инциденты:**"]
                for inc in incidents:
                    status_emoji = "🔄" if inc.get("status") == "recovering" else "⚠️"
                    server = inc.get("server_handle", "unknown")
                    inc_type = inc.get("incident_type", "unknown")
                    attempts = inc.get("recovery_attempts", 0)
                    lines.append(
                        f"{status_emoji} Сервер *{server}* — {inc_type} "
                        f"(попыток восстановления: {attempts})"
                    )
                lines.append("\n_Автовосстановление запущено. Новые деплои могут быть отложены._")
                response_parts.insert(0, "\n".join(lines))  # Insert at beginning
            continue

        if tool_name == "create_project_intent":
            po_intent = result.get("intent")
            project_intent = result
            if po_intent and po_intent != "new_project":
                response_parts.append(
                    "Для обновления проекта нужен его ID. Укажите ID, и я продолжу."
                )
            continue

        if tool_name == "list_projects":
            projects = result or []
            if not projects:
                response_parts.append("Проектов пока нет.")
            else:
                lines = ["Проекты:"]
                lines.extend(_format_project_line(project) for project in projects)
                response_parts.append("\n".join(lines))
            continue

        if tool_name == "get_project_status":
            if result:
                response_parts.append(
                    "\n".join(
                        [
                            "Статус проекта:",
                            _format_project_line(result),
                        ]
                    )
                )
            else:
                response_parts.append("Проект не найден.")
            continue

        if tool_name == "set_project_maintenance":
            if result.get("error"):
                response_parts.append(f"Ошибка: {result['error']}")
            else:
                po_intent = "maintenance"
                project_intent = {
                    "intent": "maintenance",
                    "project_id": result.get("id"),
                    "maintenance_request": result.get("config", {}).get("maintenance_request"),
                }
                response_parts.append(
                    f"Проект {result.get('name')} переведён в режим обслуживания. "
                    "Запускаю Engineering flow..."
                )
            continue

    current_project = None
    if project_intent and project_intent.get("project_id"):
        current_project = project_intent["project_id"]

    # Build messages: always include ToolMessages for tool_call responses,
    # then optionally add AIMessage with formatted response
    messages = tool_results
    if response_parts:
        messages = tool_results + [AIMessage(content="\n\n".join(response_parts))]

    updates = {
        "messages": messages,
        "po_intent": po_intent,
        "project_intent": project_intent,
        "current_project": current_project,
    }

    return updates

