from shared.contracts.base import BaseMessage


class QAMessage(BaseMessage):
    """Trigger QA testing for a deployed project."""

    story_id: str
    project_id: str
    user_id: str
    deployed_url: str
    bot_username: str | None = None
    qa_attempt: int = 0
