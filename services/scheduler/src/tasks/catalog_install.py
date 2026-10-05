"""Keep automatic coding repairs out of a mechanical installation story."""

from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskStatus, TaskType


async def refuse_install_coding_fallback(api, story_id, detail, *, tasks=None) -> bool:
    if tasks is None:
        tasks = await api.get_tasks_by_story(story_id)
    if not any(
        task.type is TaskType.INSTALL and task.status is not TaskStatus.CANCELLED for task in tasks
    ):
        return False
    await api.stop_story(
        story_id,
        "human-review",
        StoryFailure(
            code=StoryFailureCode.SCAFFOLD_FAILED,
            source="scheduler",
            detail=(
                f"Catalog installation requires review: {detail}. No automatic engineering repair."
            ),
        ),
        actor="scheduler",
    )
    return True
