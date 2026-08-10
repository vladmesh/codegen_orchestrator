from shared.contracts.base import BaseMessage


class ArchitectMessage(BaseMessage):
    """Trigger architect decomposition for a story.

    Published by create_story PO tool, consumed by architect consumer in scheduler.
    Architect decomposes the story into tasks with dependency chains.
    """

    story_id: str
    project_id: str
    # Telegram chat of the project owner, resolved by the producer. Empty when
    # the work was started by the system and has no user to report back to.
    telegram_chat_id: str = ""
    is_reopen: bool = False
    user_report: str | None = None
