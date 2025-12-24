"""Brainstorm agent node.

First node in the orchestrator graph. Gathers requirements from user,
asks clarifying questions, and creates project spec.
"""

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from ..tools.database import create_project

SYSTEM_PROMPT = """You are Brainstorm, the first agent in the codegen orchestrator.

Your job:
1. Understand what project the user wants to create
2. Ask clarifying questions if requirements are unclear (max 2-3 rounds)
3. When requirements are clear, create the project using the create_project tool

## Available modules (from service-template):
- **backend**: FastAPI REST API with PostgreSQL
- **telegram_worker**: Telegram bot message handler
- **notifications_worker**: Background notifications processor

## Entry points:
- **telegram**: Needs a Telegram bot. **YOU MUST ASK FOR THE TELEGRAM BOT TOKEN**.
- **frontend**: Web UI (needs domain allocation)
- **api**: REST API (needs port allocation)

## Guidelines:
- Ask about: main functionality, which entry points needed, any external APIs
- **If user wants a Telegram bot, explicitly ask for the Bot Token.**
- Project name should be snake_case (e.g., weather_bot)
- When ready, call create_project with all gathered info (including telegram_token if applicable)
- Respond in the SAME LANGUAGE as the user

## Example conversation:
User: "Создай бота для погоды"
You: "Отлично! Пара уточнений:
1. Бот будет получать погоду по городу от пользователя?
2. Нужен ли веб-интерфейс?
3. Пожалуйста, предоставьте Telegram Bot Token для настройки."

User: "Да, по городу. Веб не нужен. Токен: 123:ABC..."
You: *calls create_project with telegram_token='123:ABC...'*
"""

# LLM with tools
llm = ChatOpenAI(model="gpt-4o", temperature=0.7)
llm_with_tools = llm.bind_tools([create_project])


async def run(state: dict) -> dict:
    """Run brainstorm agent.

    Analyzes user request and creates project specification.
    Uses LLM to have a conversation and gather requirements.
    """
    messages = state.get("messages", [])

    # Build message list with system prompt
    llm_messages = [SystemMessage(content=SYSTEM_PROMPT)]
    llm_messages.extend(messages)

    # Invoke LLM
    response = await llm_with_tools.ainvoke(llm_messages)

    return {
        "messages": [response],
        "current_agent": "brainstorm",
    }


async def execute_tools(state: dict) -> dict:
    """Execute tool calls from Brainstorm LLM.

    Processes create_project tool call and updates state.
    """
    messages = state.get("messages", [])
    last_message = messages[-1]

    if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
        return {"messages": []}

    tool_results = []
    created_project = None

    for tool_call in last_message.tool_calls:
        if tool_call["name"] == "create_project":
            # Execute the tool
            result = await create_project.ainvoke(tool_call["args"])
            created_project = result

            tool_results.append(
                ToolMessage(
                    content=f"Project created: {result}",
                    tool_call_id=tool_call["id"],
                )
            )

    updates = {"messages": tool_results}

    if created_project:
        updates["current_project"] = created_project.get("id")
        updates["project_spec"] = created_project.get("config", {})

    return updates
