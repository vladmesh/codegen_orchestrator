"""Keep automatic coding repairs out of a mechanical installation story."""

from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskStatus, TaskType


async def refuse_install_coding_fallback(api, story_id, detail, *, tasks=None) -> bool:
    if tasks is None:
        tasks = await api.get_tasks_by_story(story_id)
    live = [task for task in tasks if task.status is not TaskStatus.CANCELLED]
    # A story that also carries engineering tasks already paid for coding: its CI,
    # conflict and QA failures take the ordinary repair route like any feature story.
    if not any(task.type is TaskType.INSTALL for task in live) or any(
        task.type is not TaskType.INSTALL for task in live
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
