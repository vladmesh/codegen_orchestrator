"""The user's answer to checks QA could not run is kept on the story, every answer of it."""

import uuid

from fastapi import status
import pytest

from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.queues.qa import QAMessage

EMAIL = {
    "name": "Telegram: the reminder email reaches the user",
    "reason": "needs an email inbox",
    "origin": "executor",
}
SETTLED = {
    "qa_outcome": "passed",
    "summary": "the bot answers /start",
    "passed_checks": ["Telegram: /start replies with a welcome"],
    "unverified_checks": [EMAIL],
}


async def _completed_story_with_unverified_check(async_client) -> tuple[str, str]:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"answer_{telegram_id}"}
    )
    assert user.status_code == status.HTTP_201_CREATED
    project_id = str(uuid.uuid4())
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "initiating_run_id": f"init-{uuid.uuid4().hex[:8]}",
            "title": "Unverified answers",
            "config": {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.status_code == status.HTTP_201_CREATED
    story = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Reminders"}
    )
    assert story.status_code == status.HTTP_201_CREATED, story.text
    story_id = story.json()["id"]
    assert story.json()["unverified_decisions"] == []
    for action in ("start", "deploy", "test"):
        moved = await async_client.post(f"/api/stories/{story_id}/{action}")
        assert moved.status_code == status.HTTP_200_OK, moved.text

    run_id = f"qa-{uuid.uuid4().hex[:8]}"
    handoff = QAHandoffPlan(
        qa_message=QAMessage(
            story_id=story_id,
            project_id=project_id,
            initiating_run_id="live-1",
            deployed_url="https://answers.example.com",
            application_id=42,
            acceptance_criteria="the bot answers /start",
            run_id=run_id,
        )
    ).model_dump(mode="json")
    run = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": run_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {QA_HANDOFF_KEY: handoff},
        },
    )
    assert run.status_code == status.HTTP_200_OK, run.text
    settled = await async_client.patch(
        f"/api/runs/{run_id}", json={"status": "completed", "result": SETTLED}
    )
    assert settled.status_code == status.HTTP_200_OK, settled.text
    completed = await async_client.post(
        f"/api/stories/{story_id}/complete", json={"qa_run_id": run_id}
    )
    assert completed.status_code == status.HTTP_200_OK, completed.text
    return story_id, run_id


@pytest.mark.asyncio
async def test_each_answer_is_appended_and_read_back(async_client) -> None:
    story_id, run_id = await _completed_story_with_unverified_check(async_client)

    first = await async_client.post(
        f"/api/stories/{story_id}/unverified-decisions",
        json={
            "decision": "change_requirement",
            "check_names": [EMAIL["name"]],
            "recorded_by": "po",
        },
    )
    later = await async_client.post(
        f"/api/stories/{story_id}/unverified-decisions",
        json={
            "decision": "accept_unverified",
            "check_names": [EMAIL["name"]],
            "recorded_by": "po",
        },
    )

    assert first.status_code == status.HTTP_200_OK, first.text
    assert later.status_code == status.HTTP_200_OK, later.text
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    decisions = story["unverified_decisions"]
    assert [d["decision"] for d in decisions] == ["change_requirement", "accept_unverified"]
    assert all(d["qa_run_id"] == run_id for d in decisions)
    assert all(d["check_names"] == [EMAIL["name"]] for d in decisions)
    assert all(d["recorded_by"] == "po" for d in decisions)
    assert decisions[0]["decided_at"] <= decisions[1]["decided_at"]
    # An answer changes nothing else about the story.
    assert story["status"] == "completed"


@pytest.mark.asyncio
async def test_a_check_the_run_did_not_leave_unverified_is_refused(async_client) -> None:
    story_id, _ = await _completed_story_with_unverified_check(async_client)

    refused = await async_client.post(
        f"/api/stories/{story_id}/unverified-decisions",
        json={"decision": "accept_unverified", "check_names": ["invented"], "recorded_by": "po"},
    )

    assert refused.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["unverified_decisions"] == []
