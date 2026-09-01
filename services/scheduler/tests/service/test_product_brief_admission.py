"""Real API and scheduler proof for Product Brief dispatch admission."""

import uuid

import pytest

from src.tasks.task_dispatcher import dispatch_todo_tasks


class _RecordingRedis:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def publish_message(self, queue: str, message: object) -> str:
        self.messages.append((queue, message))
        return "1-1"


@pytest.mark.asyncio
async def test_brief_backed_todo_stays_out_of_scheduler_until_admitted(api_client):
    """The real API flag, not the task status, controls scheduler selection."""
    suffix = uuid.uuid4().hex
    telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    assert (
        await api_client.request(
            "POST",
            "users/",
            json={"telegram_id": telegram_id, "username": f"brief-dispatch-{suffix}"},
        )
    ).is_success
    assert (
        await api_client.request(
            "POST",
            "projects/",
            json={
                "id": project_id,
                "title": "Brief scheduler admission",
                "initiating_run_id": f"init-{suffix}",
                "status": "active",
                "config": {"workspace_ready": True},
            },
            headers={"X-Telegram-ID": str(telegram_id)},
        )
    ).is_success

    content = {
        "intended_users": ["Bilingual customers"],
        "languages": ["ru", "en"],
        "must_requirements": [
            {"id": "must-language", "text": "Support Russian and English.", "source": "User"}
        ],
        "initial_settings": [{"key": "languages", "value": ["ru", "en"], "scope": "product"}],
    }
    brief = await api_client.request(
        "POST",
        "product-briefs/",
        json={
            "project_id": project_id,
            "title": "Bilingual product",
            "content": content,
            "request_id": f"brief-create-{suffix}",
        },
    )
    assert brief.is_success, brief.text
    brief_id = brief.json()["id"]
    confirmed = await api_client.request(
        "POST",
        f"product-briefs/{brief_id}/confirm",
        json={"request_id": f"brief-confirm-{suffix}", "content": content},
    )
    assert confirmed.is_success, confirmed.text
    story = await api_client.request(
        "POST",
        "stories/",
        json={
            "project_id": project_id,
            "title": "Bilingual Story",
            "type": "product",
            "product_brief_id": brief_id,
        },
    )
    assert story.is_success, story.text
    task = await api_client.create_task(
        {
            "project_id": project_id,
            "story_id": story.json()["id"],
            "title": "Implement bilingual UI",
            "status": "todo",
            "created_by": "architect",
        }
    )
    assert task.dispatch_admitted is False

    redis = _RecordingRedis()
    assert await dispatch_todo_tasks(api_client, redis) == 0
    assert redis.messages == []

    coverage = await api_client.request(
        "PUT",
        f"product-briefs/{brief_id}/coverage/must-language",
        json={"requirement_id": "must-language", "task_id": task.id},
    )
    assert coverage.is_success, coverage.text
    admitted = await api_client.request("POST", f"product-briefs/{brief_id}/admit")
    assert admitted.is_success, admitted.text
    assert admitted.json()["outcome"] == "admitted"

    assert await dispatch_todo_tasks(api_client, redis) == 1
    assert len(redis.messages) == 1
