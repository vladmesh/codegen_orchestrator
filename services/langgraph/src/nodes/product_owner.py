"""Product Owner (PO) agent node.

Classifies user intent and coordinates the high-level flow.

Uses BaseAgentNode for dynamic prompt loading from database.
Note: execute_tools has custom logic for response formatting, not using BaseAgentNode.execute_tools.
"""

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from ..tools import (
    activate_project,
    check_ready_to_deploy,
    create_project_intent,
    get_project_status,
    inspect_repository,
    list_active_incidents,
    list_managed_servers,
    list_projects,
    list_resource_inventory,
    save_project_secret,
    set_project_maintenance,
)
from .base import BaseAgentNode

# Tools available to PO
tools = [
    list_active_incidents,
    list_projects,
    list_managed_servers,
    get_project_status,
    create_project_intent,
    set_project_maintenance,
    # Activation flow tools
    activate_project,
    inspect_repository,
    save_project_secret,
    check_ready_to_deploy,
    list_resource_inventory,
]

tools_map = {tool.name: tool for tool in tools}


class ProductOwnerNode(BaseAgentNode):
    """Product Owner agent that classifies intent and coordinates flow."""

    pass


# Create singleton instance
_node = ProductOwnerNode("product_owner", tools)


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

    # Get dynamic prompt from database
    system_prompt = await _node.get_system_prompt()

    llm_messages = [SystemMessage(content=system_prompt)]
    llm_messages.extend(messages)

    # Get configured LLM
    llm_with_tools = await _node.get_llm_with_tools()

    response = await llm_with_tools.ainvoke(llm_messages)
    return {
        "messages": [response],
        "current_agent": "product_owner",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Product Owner LLM.

    Note: This has custom logic for response formatting that differs
    from BaseAgentNode.execute_tools. Kept separate for compatibility.
    """
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    tool_results = []
    response_parts = []
    po_intent = state.get("po_intent")
    project_intent = state.get("project_intent")
    repo_info = state.get("repo_info")  # Track repo_info for discovered projects

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

        # Wrap tool execution in try/except to always return ToolMessage
        try:
            result = await tool_func.ainvoke(tool_call["args"])
        except Exception as e:
            # On error, return error as ToolMessage to prevent broken sequences
            tool_results.append(
                ToolMessage(
                    content=f"Error executing {tool_name}: {e!s}",
                    tool_call_id=tool_call["id"],
                )
            )
            response_parts.append(f"⚠️ Ошибка при выполнении {tool_name}: {e!s}")
            continue

        tool_results.append(
            ToolMessage(
                content=f"Result: {result}",
                tool_call_id=tool_call["id"],
            )
        )

        # Convert Pydantic models to dicts for easier handling in existing logic
        if isinstance(result, BaseModel):
            result = result.model_dump()
        elif isinstance(result, list):
            result = [item.model_dump() if isinstance(item, BaseModel) else item for item in result]

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
                lines = ["📦 **Проекты:**"]
                lines.extend(_format_project_line(project) for project in projects)
                response_parts.append("\n".join(lines))
            continue

        if tool_name == "list_managed_servers":
            servers = result or []
            if not servers:
                response_parts.append("Активных серверов нет.")
            else:
                lines = ["🖥️ **Серверы:**"]
                for srv in servers:
                    handle = srv.get("handle", "unknown")
                    status = srv.get("status", "unknown")
                    ip = srv.get("public_ip", "")
                    ram_total = srv.get("capacity_ram_mb", 0)
                    ram_used = srv.get("used_ram_mb", 0)
                    status_emoji = "✅" if status in ("ready", "in_use") else "⚠️"
                    lines.append(
                        f"{status_emoji} *{handle}* [{status}] — {ip} "
                        f"(RAM: {ram_used}/{ram_total} MB)"
                    )
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

        # === Activation Flow Tools ===

        if tool_name == "activate_project":
            if result.get("error"):
                response_parts.append(f"Ошибка: {result['error']}")
            else:
                project_name = result.get("project_name", result.get("project_id"))
                missing = result.get("missing_secrets", [])
                current_project = result.get("project_id")
                # Extract repo_info for DevOps
                if result.get("repo_info"):
                    repo_info = result["repo_info"]

                if missing:
                    secrets_list = ", ".join(f"`{s}`" for s in missing)
                    response_parts.append(
                        f"🔧 Проект **{project_name}** активирован для деплоя.\n\n"
                        f"Для запуска нужны секреты: {secrets_list}\n\n"
                        "Пожалуйста, пришлите значение для каждого."
                    )
                else:
                    # All secrets configured - auto-trigger deploy!
                    po_intent = "deploy"
                    project_intent = {
                        "intent": "deploy",
                        "project_id": current_project,
                    }
                    response_parts.append(
                        f"✅ Проект **{project_name}** готов к деплою! "
                        "Все секреты настроены.\n\n🚀 Запускаю деплой..."
                    )
            continue

        if tool_name == "save_project_secret":
            if result.get("error"):
                response_parts.append(f"Ошибка: {result['error']}")
            else:
                key = result.get("key")
                missing = result.get("missing_secrets", [])
                project_id = result.get("project_id")

                if missing:
                    secrets_list = ", ".join(f"`{s}`" for s in missing)
                    response_parts.append(
                        f"✅ Секрет `{key}` сохранён.\n\nЕщё нужны: {secrets_list}"
                    )
                else:
                    # All secrets configured - auto-trigger deploy!
                    po_intent = "deploy"
                    project_intent = {
                        "intent": "deploy",
                        "project_id": project_id,
                    }
                    current_project = project_id
                    response_parts.append(
                        f"✅ Секрет `{key}` сохранён. Все секреты настроены!\n\n"
                        "🚀 Запускаю деплой..."
                    )
            continue

        if tool_name == "check_ready_to_deploy":
            if result.get("error"):
                response_parts.append(f"Ошибка: {result['error']}")
            elif result.get("ready"):
                project_name = result.get("project_name", result.get("project_id"))
                po_intent = "deploy"
                project_intent = {
                    "intent": "deploy",
                    "project_id": result.get("project_id"),
                }
                response_parts.append(f"🚀 Проект **{project_name}** готов! Запускаю деплой...")
            else:
                missing = result.get("missing", [])
                secrets_list = ", ".join(f"`{s}`" for s in missing)
                response_parts.append(f"⏳ Ещё не готов к деплою. Не хватает: {secrets_list}")
            continue

        if tool_name == "list_resource_inventory":
            servers = result.get("servers", [])
            total_projects = result.get("total_projects", 0)
            with_secrets = result.get("projects_with_secrets", 0)

            lines = ["📊 **Инвентарь ресурсов:**", ""]
            lines.append(f"**Проекты:** {total_projects} всего, {with_secrets} с секретами")
            lines.append("")

            if servers:
                lines.append("**Серверы:**")
                for srv in servers:
                    handle = srv.get("handle", "?")
                    status = srv.get("status", "?")
                    ram = srv.get("available_ram_mb", 0)
                    lines.append(f"- {handle} [{status}] — {ram} MB свободно")
            else:
                lines.append("Серверов нет.")

            response_parts.append("\n".join(lines))
            continue

        if tool_name == "inspect_repository":
            # Usually called internally, but format if called directly
            if result.get("error"):
                response_parts.append(f"Ошибка инспекции: {result['error']}")
            else:
                required = result.get("required_secrets", [])
                missing = result.get("missing_secrets", [])
                has_compose = result.get("has_docker_compose", False)

                lines = [f"📁 **Репозиторий {result.get('project_id')}:**"]
                lines.append(f"- Docker Compose: {'✅' if has_compose else '❌'}")
                lines.append(f"- Требуемые секреты: {', '.join(required) if required else 'нет'}")
                if missing:
                    lines.append(f"- ⚠️ Не настроены: {', '.join(missing)}")
                response_parts.append("\n".join(lines))
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
        "repo_info": repo_info,
    }

    return updates
