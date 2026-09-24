"""A platform failure that stops a story leaves its cause on the story, and its PO can read it.

Incident 2026-09-24: story-3990e41c's scaffold failed on clone after an architect
had already taken it to in_progress. Nothing failed the story, and its PO read
"in_progress, no tasks" for over an hour. These tests go through the real
database: the stop commits the typed reason and the owed notices with the
transition, the diagnostics read returns the causes the platform recorded, and
the state-age ending of a planless in_progress story compares on locked rows.
"""

from datetime import UTC, datetime, timedelta
import uuid

from httpx import AsyncClient
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.state_wait import (
    StateWaitAnchor,
    StateWaitEnding,
    StateWaitExpiryCommand,
    StateWaitExpiryReason,
    StateWaitSkipReason,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.models import Project, Story

TOKEN = "ghs_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6"  # noqa: S105 - a fake installation token


async def _project(client: AsyncClient, config: dict | None = None) -> str:
    project_id = str(uuid.uuid4())
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"failure_{telegram_id}"},
    )
    assert user.status_code == 201, user.text
    created = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"Failure visibility {project_id[:8]}",
            "initiating_run_id": f"run-{project_id[:8]}",
            "status": "draft",
            "config": config or {},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created.status_code == 201, created.text
    return project_id


async def _started_story(client: AsyncClient, project_id: str) -> str:
    created = await client.post("/api/stories/", json={"project_id": project_id, "title": "Canary"})
    assert created.status_code == 201, created.text
    story_id = created.json()["id"]
    started = await client.post(f"/api/stories/{story_id}/start", json={"actor": "architect"})
    assert started.status_code == 200, started.text
    return story_id


async def _set_project_config(db: AsyncSession, project_id: str, config: dict) -> None:
    await db.execute(
        update(Project).where(Project.id == uuid.UUID(project_id)).values(config=config)
    )
    await db.commit()


def _failure(detail: str) -> dict:
    return {"code": "scaffold_failed", "source": "scaffolder", "detail": detail}


@pytest.mark.asyncio
async def test_failing_a_story_with_a_reason_stores_it_and_owes_both_audiences(
    async_client: AsyncClient,
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    detail = f"Git init/fetch failed: https://x-access-token:{TOKEN}@github.com/o/p not found"

    failed = await async_client.post(
        f"/api/stories/{story_id}/fail",
        json={"actor": "scaffolder", "failure": _failure(detail)},
    )

    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["status"] == "failed"
    reason = body["quarantine_reason"]
    assert reason["reason"] == "story_failure"
    assert reason["code"] == "scaffold_failed"
    assert TOKEN not in reason["detail"]
    record = OwnerNotification.model_validate(
        (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json()
    )
    assert record.event is OwnerNotificationEvent.STORY_FAILED
    assert record.terminal_status is StoryStatus.FAILED
    assert record.state is OwnerNotificationState.OWED
    assert record.admin_state is OwnerNotificationState.OWED
    assert "could not create the project's code repository" in record.text
    assert TOKEN not in record.text
    owed = await async_client.get("/api/stories/owner-notifications/owed", params={"limit": 500})
    assert story_id in {row["id"] for row in owed.json()}


@pytest.mark.asyncio
async def test_parking_a_story_with_a_reason_owes_the_blocked_notice(async_client: AsyncClient):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)

    parked = await async_client.post(
        f"/api/stories/{story_id}/human-review",
        json={
            "actor": "architect",
            "failure": {
                "code": "scaffold_timeout",
                "source": "architect",
                "detail": "still not ready after 300 seconds",
            },
        },
    )

    assert parked.status_code == 200, parked.text
    assert parked.json()["status"] == "waiting_human_review"
    record = OwnerNotification.model_validate(
        (await async_client.get(f"/api/stories/{story_id}/owner-notification")).json()
    )
    assert record.event is OwnerNotificationEvent.STORY_BLOCKED
    assert record.terminal_status is StoryStatus.WAITING_HUMAN_REVIEW


@pytest.mark.asyncio
async def test_a_refused_stop_writes_no_reason(async_client: AsyncClient):
    project_id = await _project(async_client)
    created = await async_client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Archived"}
    )
    story_id = created.json()["id"]
    assert (await async_client.post(f"/api/stories/{story_id}/archive")).status_code == 200

    refused = await async_client.post(
        f"/api/stories/{story_id}/fail",
        json={"actor": "scaffolder", "failure": _failure("clone failed")},
    )

    assert refused.status_code == 422
    story = (await async_client.get(f"/api/stories/{story_id}")).json()
    assert story["status"] == "archived"
    assert story["quarantine_reason"] is None


@pytest.mark.asyncio
async def test_diagnostics_return_the_recorded_causes_without_logs_by_request(
    async_client: AsyncClient, db_session: AsyncSession
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    await _set_project_config(
        db_session, project_id, {"scaffold_error": "Repository not found " + "x" * 2000}
    )

    read = await async_client.get(
        f"/api/stories/{story_id}/diagnostics", params={"include_logs": "false"}
    )

    assert read.status_code == 200, read.text
    body = read.json()
    assert body["story_status"] == "in_progress"
    assert body["project_status"] == "draft"
    assert body["scaffold_error"].startswith("Repository not found")
    assert len(body["scaffold_error"]) <= 503
    assert body["work_cycle_tasks"] == 0
    assert body["failure"] is None
    assert body["logs"] == []
    assert body["logs_unavailable"] == "not requested"


@pytest.mark.asyncio
async def test_diagnostics_of_a_failed_story_carry_its_typed_failure(async_client: AsyncClient):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    await async_client.post(
        f"/api/stories/{story_id}/fail",
        json={"actor": "scaffolder", "failure": _failure("clone failed")},
    )

    body = (
        await async_client.get(
            f"/api/stories/{story_id}/diagnostics", params={"include_logs": "false"}
        )
    ).json()

    assert body["failure"]["code"] == "scaffold_failed"
    assert body["failure"]["detail"] == "clone failed"
    assert body["quarantine_reason"] is None


@pytest.mark.asyncio
async def test_diagnostics_are_refused_to_a_user_who_does_not_own_the_project(
    async_client: AsyncClient,
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    stranger = 770000000 + int(uuid.uuid4().int % 1000000)
    created = await async_client.post(
        "/api/users/", json={"telegram_id": stranger, "username": f"s{stranger}"}
    )
    assert created.status_code in {200, 201}, created.text

    read = await async_client.get(
        f"/api/stories/{story_id}/diagnostics",
        params={"include_logs": "false"},
        headers={"X-Telegram-ID": str(stranger), "X-Internal-Key": ""},
    )

    assert read.status_code in {401, 403}


def _planless_command(story_id: str, project_id: str, anchored_at: datetime):
    record = OwnerNotification(
        event=OwnerNotificationEvent.STORY_BLOCKED,
        text="Work on this change has not started.",
        story_id=story_id,
        project_id=project_id,
        terminal_status=StoryStatus.WAITING_HUMAN_REVIEW,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    )
    return StateWaitExpiryCommand(
        expected_status=StoryStatus.IN_PROGRESS,
        ending=StateWaitEnding.PARK,
        anchor=StateWaitAnchor(story_updated_at=anchored_at),
        reason=StateWaitExpiryReason(
            status=StoryStatus.IN_PROGRESS,
            waiting_on="none",
            config_key="supervisor.planless_story_max_minutes",
            threshold_minutes=60,
            anchor="story_updated_at_without_tasks",
            anchor_at=anchored_at.isoformat(),
            age_minutes=61.0,
            ending=StateWaitEnding.PARK,
        ),
        owner_notification=record,
    )


async def _story_row(db: AsyncSession, story_id: str) -> Story:
    db.expire_all()
    story = await db.get(Story, story_id)
    assert story is not None
    return story


@pytest.mark.asyncio
async def test_a_planless_in_progress_story_is_parked_by_the_guarded_ending(
    async_client: AsyncClient, db_session: AsyncSession
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    anchored_at = (await _story_row(db_session, story_id)).updated_at

    ended = await async_client.post(
        f"/api/stories/{story_id}/expire-state-wait",
        json=_planless_command(story_id, project_id, anchored_at).model_dump(mode="json"),
    )

    assert ended.status_code == 200, ended.text
    assert ended.json()["disposition"] == "expired"
    story = await _story_row(db_session, story_id)
    assert story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
    assert story.quarantine_reason["anchor"] == "story_updated_at_without_tasks"


@pytest.mark.asyncio
async def test_a_planless_ending_is_skipped_once_a_task_exists(
    async_client: AsyncClient, db_session: AsyncSession
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    anchored_at = (await _story_row(db_session, story_id)).updated_at
    task = await async_client.post(
        "/api/tasks/",
        json={"project_id": project_id, "story_id": story_id, "title": "Plan", "status": "todo"},
    )
    assert task.status_code == 201, task.text

    ended = await async_client.post(
        f"/api/stories/{story_id}/expire-state-wait",
        json=_planless_command(story_id, project_id, anchored_at).model_dump(mode="json"),
    )

    assert ended.json()["disposition"] == "skipped"
    assert ended.json()["skip"]["reason"] == StateWaitSkipReason.TASKS_CREATED.value
    assert (await _story_row(db_session, story_id)).status == StoryStatus.IN_PROGRESS.value


@pytest.mark.asyncio
async def test_a_planless_ending_is_skipped_when_the_story_row_moved(
    async_client: AsyncClient, db_session: AsyncSession
):
    project_id = await _project(async_client)
    story_id = await _started_story(async_client, project_id)
    anchored_at = (await _story_row(db_session, story_id)).updated_at - timedelta(minutes=5)

    ended = await async_client.post(
        f"/api/stories/{story_id}/expire-state-wait",
        json=_planless_command(story_id, project_id, anchored_at).model_dump(mode="json"),
    )

    assert ended.json()["disposition"] == "skipped"
    assert ended.json()["skip"]["reason"] == StateWaitSkipReason.STORY_UPDATED.value
