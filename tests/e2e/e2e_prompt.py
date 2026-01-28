"""
E2E Test Prompts

Deterministic prompts for E2E testing that force Claude PO
to execute commands without asking clarifying questions.
"""


def build_project_creation_prompt(
    project_name: str,
    telegram_token: str = "123456:ABC-TEST-TOKEN",  # noqa: S107
) -> str:
    """Build deterministic prompt that forces PO to create project immediately.

    Args:
        project_name: Unique project name for the test.
        telegram_token: Fake telegram token for testing.

    Returns:
        Prompt string that instructs Claude to create project without questions.
    """
    return f"""Создай проект телеграм-бота, который на любое сообщение отвечает "Hello".

Параметры:
- Название проекта: {project_name}
- Модули: backend, telegram
- Telegram токен: {telegram_token}

ВАЖНО:
- НЕ задавай уточняющих вопросов
- Сразу выполни команду создания проекта
- После создания проекта запусти engineering командой
- Сообщи результат через orchestrator respond

Ожидаемые команды:
1. orchestrator project create --name {project_name}
2. orchestrator engineering trigger <project_id из шага 1>
3. orchestrator respond "Проект создан и engineering запущен"
"""


def build_simple_project_prompt(project_name: str) -> str:
    """Build minimal prompt for project creation only (no engineering).

    Args:
        project_name: Unique project name for the test.

    Returns:
        Prompt string for simple project creation.
    """
    return f"""Создай проект с названием "{project_name}".

ВАЖНО: НЕ задавай вопросов. Сразу выполни:
1. orchestrator project create --name {project_name}
2. orchestrator respond "Проект создан"
"""


# Expected output patterns for validation
EXPECTED_PATTERNS = {
    "project_create": r"orchestrator\s+project\s+create\s+--name\s+\S+",
    "engineering_trigger": r"orchestrator\s+engineering\s+trigger\s+\S+",
    "respond": r"orchestrator\s+respond\s+",
}
