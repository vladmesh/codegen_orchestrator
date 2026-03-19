from shared.contracts.base import BaseMessage


class ArchitectMessage(BaseMessage):
    """Trigger architect decomposition for a story.

    Published by create_story PO tool, consumed by architect consumer in scheduler.
    Architect decomposes the story into tasks with dependency chains.
    """

    story_id: str
    project_id: str
    user_id: str
    is_reopen: bool = False
    user_report: str | None = None
