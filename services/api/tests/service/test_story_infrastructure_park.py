"""Service proofs for the atomic park of a pre-agent infrastructure refusal.

Two writers share one park function. Admission parks the paid refusal it decides
in the same transaction that audits it; the park endpoint parks only what a
locked refused Run, or the unique committed admission audit, proves. Every test
goes through the real database transaction and reads the committed rows back, so
a partial park or an unproved quarantine is observable here.
"""

import asyncio
from datetime import UTC, datetime, timedelta
import uuid

from httpx import AsyncClient
import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_dispatch import EngineeringDispatchRefusal
from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAuthMode,
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
    safe_executor_diagnostic_reason,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import AgentType
from shared.models import Run, Story, WorkAdmissionAudit
from src.routers._story_helpers import _land_on

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
KEY = "engineering_infrastructure"
RUN_DETAIL = "Engineering worker creation was refused: project locked."
AUDIT_MESSAGE = "The selected executor is unavailable."
PARK_ACTION = "park_infrastructure_refusal"
ADMISSION_URL = "/api/work-admission/engineering-dispatches"


# --- fixtures of state ------------------------------------------------------


async def _story_task(
    client: AsyncClient, *, task_status: str = "todo", project_id: str = PROJECT_ID
) -> tuple[str, str]:
    created_story = await client.post(
        "/api/stories/", json={"project_id": project_id, "title": "Infrastructure park"}
    )
    assert created_story.status_code == 201, created_story.text
    story_id = created_story.json()["id"]
    created_task = await client.post(
        "/api/tasks/",
        json={
            "project_id": project_id,
            "story_id": story_id,
            "title": "Refused task",
            "status": "todo",
        },
    )
    assert created_task.status_code == 201, created_task.text
    task_id = created_task.json()["id"]
    started = await client.post(f"/api/stories/{story_id}/start", json={"actor": "test"})
    assert started.status_code == 200, started.text
    for hop in {"todo": [], "in_dev": ["in_dev"], "failed": ["in_dev", "failed"]}[task_status]:
        moved = await client.post(
            f"/api/tasks/{task_id}/transition?to_status={hop}", json={"actor": "test"}
        )
        assert moved.status_code == 200, moved.text
    return story_id, task_id


async def _refused_run(
    db_session: AsyncSession,
    story_id: str,
    task_id: str,
    *,
    execution: dict | None = None,
) -> str:
    run_id = f"eng-refused-{uuid.uuid4().hex[:10]}"
    db_session.add(
        Run(
            id=run_id,
            type="engineering",
            status="failed",
            project_id=uuid.UUID(PROJECT_ID),
            task_id=task_id,
            story_id=story_id,
            run_metadata={},
            result={
                "engineering_status": "failed",
                "execution": execution
                or {
                    "execution_phase": "pre_agent_refused",
                    "infrastructure_refusal": "project_locked",
                },
            },
        )
    )
    await db_session.commit()
    return run_id


async def _admission_audit(  # noqa: PLR0913
    db_session: AsyncSession,
    *,
    attempt_id: str,
    task_id: str,
    story_id: str,
    iteration: int = 0,
    reason: str = "executor_unavailable",
    message: str = AUDIT_MESSAGE,
) -> None:
    db_session.add(
        WorkAdmissionAudit(
            subject="paid_work",
            outcome="denied",
            reason=reason,
            reference_id=attempt_id,
            message=message,
            command_payload={
                "id": attempt_id,
                "type": "engineering",
                "project_id": PROJECT_ID,
                "task_id": task_id,
                "story_id": story_id,
                "run_metadata": {"iteration": iteration},
            },
        )
    )
    await db_session.commit()


def _run_park(task_id: str, attempt_id: str, **changes) -> dict:
    return {
        "execution_phase": "pre_agent_refused",
        "refusal": "project_locked",
        "task_id": task_id,
        "attempt_id": attempt_id,
        "detail": RUN_DETAIL,
        **changes,
    }


def _audit_park(task_id: str, attempt_id: str) -> dict:
    return _run_park(task_id, attempt_id, refusal="executor_unavailable", detail=AUDIT_MESSAGE)


async def _park_call(client: AsyncClient, story_id: str, park: dict):
    return await client.post(
        f"/api/stories/{story_id}/park-infrastructure-refusal",
        json={"park": park, "actor": "supervisor"},
    )


async def _state(client: AsyncClient, story_id: str, task_id: str) -> tuple:
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    story = (await client.get(f"/api/stories/{story_id}")).json()
    return (
        task["status"],
        task["failure_metadata"],
        task["current_iteration"],
        story["status"],
        story["quarantine_reason"],
    )


async def _park_hops(client: AsyncClient, task_id: str) -> list[str]:
    events = (await client.get(f"/api/tasks/{task_id}/events")).json()
    return sorted(
        event["to_status"]
        for event in events
        if (event["details"] or {}).get("action") == PARK_ACTION
    )


async def _notice(client: AsyncClient, story_id: str) -> dict | None:
    response = await client.get(f"/api/stories/{story_id}/owner-notification")
    if response.status_code == 404:
        return None
    assert response.status_code == 200, response.text
    return response.json()


def _assert_both_audiences_owed(notice: dict, story_id: str, text: str) -> None:
    assert (notice["event"], notice["state"], notice["text"], notice["terminal_status"]) == (
        "story_blocked",
        "owed",
        text,
        "waiting_human_review",
    )
    assert notice["admin_state"] == "owed"
    assert story_id in notice["admin_text"]


# --- the Run-backed endpoint ---------------------------------------------------


@pytest.mark.parametrize(
    "task_status,hops",
    [
        ("todo", ["in_dev", "waiting_human_review"]),
        ("in_dev", ["waiting_human_review"]),
        ("failed", ["waiting_human_review"]),
    ],
)
@pytest.mark.asyncio
async def test_park_commits_both_rows_evidence_and_both_audiences_together(
    async_client: AsyncClient,
    db_session: AsyncSession,
    _tasks_project,
    task_status: str,
    hops: list[str],
) -> None:
    story_id, task_id = await _story_task(async_client, task_status=task_status)
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))

    response = await _park_call(async_client, story_id, park)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["disposition"], body["task_status"], body["story_status"]) == (
        "parked",
        "waiting_human_review",
        "waiting_human_review",
    )
    assert await _state(async_client, story_id, task_id) == (
        "waiting_human_review",
        {KEY: park},
        0,
        "waiting_human_review",
        {KEY: park},
    )
    assert await _park_hops(async_client, task_id) == hops
    _assert_both_audiences_owed(await _notice(async_client, story_id), story_id, RUN_DETAIL)

    # The committed park is exactly what the one operator recovery action accepts.
    retried = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt",
        json={
            "task_id": task_id,
            "attempt_id": park["attempt_id"],
            "refusal": "project_locked",
            "actor": "admin",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["outcome"] == "retried"
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)


@pytest.mark.asyncio
async def test_repeating_the_same_park_is_a_typed_noop_that_reowes_nothing(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))

    first = await _park_call(async_client, story_id, park)
    notice = await _notice(async_client, story_id)
    repeated = await _park_call(async_client, story_id, park)

    assert [first.json()["disposition"], repeated.json()["disposition"]] == [
        "parked",
        "already_parked",
    ]
    assert await _park_hops(async_client, task_id) == ["waiting_human_review"]
    assert await _notice(async_client, story_id) == notice
    assert repeated.json()["current_iteration"] == 0


@pytest.mark.asyncio
async def test_concurrent_equal_parks_converge_to_one_park(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))

    responses = await asyncio.gather(
        _park_call(async_client, story_id, park),
        _park_call(async_client, story_id, park),
    )

    assert sorted(response.json()["disposition"] for response in responses) == [
        "already_parked",
        "parked",
    ]
    assert await _park_hops(async_client, task_id) == ["in_dev", "waiting_human_review"]
    assert (await _state(async_client, story_id, task_id))[2] == 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"attempt_id": "eng-invented"}, "refusal_evidence_missing"),
        ({"refusal": "worker_profile_unavailable"}, "stale_attempt_fence"),
        ({"detail": "A different refusal detail."}, "stale_attempt_fence"),
    ],
)
@pytest.mark.asyncio
async def test_a_park_the_refused_run_does_not_prove_fails_closed(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project, change: dict, code: str
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    before = await _state(async_client, story_id, task_id)

    response = await _park_call(async_client, story_id, {**park, **change})

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code
    assert await _state(async_client, story_id, task_id) == before
    assert await _notice(async_client, story_id) is None


@pytest.mark.asyncio
async def test_a_different_park_after_a_park_fails_closed(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    first = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    assert (await _park_call(async_client, story_id, first)).json()["disposition"] == "parked"
    second = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    before = await _state(async_client, story_id, task_id)

    response = await _park_call(async_client, story_id, second)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_infrastructure_reason"
    assert await _state(async_client, story_id, task_id) == before


@pytest.mark.asyncio
async def test_a_run_that_says_the_agent_started_proves_nothing(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    started = await _refused_run(
        db_session, story_id, task_id, execution={"execution_phase": "agent_started"}
    )

    response = await _park_call(async_client, story_id, _run_park(task_id, started))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_attempt_fence"
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, "in_progress", None)


@pytest.mark.asyncio
async def test_a_task_of_another_story_fails_closed(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, _ = await _story_task(async_client)
    other_story_id, other_task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(other_task_id, await _refused_run(db_session, other_story_id, other_task_id))

    response = await _park_call(async_client, story_id, park)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "wrong_task"
    assert await _state(async_client, other_story_id, other_task_id) == (
        "failed",
        None,
        0,
        "in_progress",
        None,
    )


@pytest.mark.asyncio
async def test_a_story_already_in_human_review_for_another_reason_fails_closed(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    reviewed = await async_client.post(f"/api/stories/{story_id}/human-review", json={"actor": "t"})
    assert reviewed.status_code == 200, reviewed.text

    response = await _park_call(async_client, story_id, park)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "already_in_human_review"
    assert await _state(async_client, story_id, task_id) == (
        "failed",
        None,
        0,
        "waiting_human_review",
        None,
    )


@pytest.mark.parametrize("action,story_status", [("fail", "failed"), ("archive", "archived")])
@pytest.mark.asyncio
async def test_a_terminal_story_is_contained_without_reopening_or_changing_the_task(
    async_client: AsyncClient,
    db_session: AsyncSession,
    _tasks_project,
    action: str,
    story_status: str,
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    ended = await async_client.post(f"/api/stories/{story_id}/{action}", json={"actor": "test"})
    assert ended.status_code == 200, ended.text

    response = await _park_call(async_client, story_id, park)

    assert response.status_code == 200, response.text
    assert response.json()["disposition"] == "ineligible_story"
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, story_status, None)
    assert await _park_hops(async_client, task_id) == []
    assert await _notice(async_client, story_id) is None


@pytest.mark.asyncio
async def test_a_story_that_turns_terminal_while_the_park_waits_is_contained(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    """The park serializes on the story row and decides on the status that committed."""
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    story = await db_session.scalar(select(Story).where(Story.id == story_id).with_for_update())

    pending = asyncio.create_task(_park_call(async_client, story_id, park))
    await asyncio.sleep(0.5)
    assert not pending.done()
    _land_on(story, StoryStatus.FAILED)
    await db_session.commit()
    response = await pending

    assert response.status_code == 200, response.text
    assert response.json()["disposition"] == "ineligible_story"
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, "failed", None)


@pytest.mark.asyncio
async def test_a_failure_inside_the_park_transaction_leaves_no_partial_park(
    async_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _tasks_project,
) -> None:
    story_id, task_id = await _story_task(async_client)
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))

    def _fail_after_task_hops(*_args, **_kwargs) -> None:
        raise RuntimeError("injected failure after the task hops")

    monkeypatch.setattr("src.routers._story_helpers._do_transition", _fail_after_task_hops)
    with pytest.raises(RuntimeError, match="injected failure"):
        await _park_call(async_client, story_id, park)
    monkeypatch.undo()

    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)
    assert await _park_hops(async_client, task_id) == []
    assert await _notice(async_client, story_id) is None
    assert (await _park_call(async_client, story_id, park)).json()["disposition"] == "parked"


@pytest.mark.asyncio
async def test_a_non_admin_user_cannot_park(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created = await async_client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"park-{telegram_id}"}
    )
    assert created.status_code == 201, created.text

    response = await async_client.post(
        f"/api/stories/{story_id}/park-infrastructure-refusal",
        json={"park": park, "actor": "user"},
        headers={"X-Telegram-ID": str(telegram_id)},
    )

    assert response.status_code == 403
    assert await _state(async_client, story_id, task_id) == ("failed", None, 0, "in_progress", None)


# --- a no-Run park is proved only by its unique admission audit ---------------------


@pytest.mark.asyncio
async def test_a_no_run_park_matching_its_admission_audit_parks(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    attempt_id = f"eng-audited-{uuid.uuid4().hex[:10]}"
    await _admission_audit(db_session, attempt_id=attempt_id, task_id=task_id, story_id=story_id)

    response = await _park_call(async_client, story_id, _audit_park(task_id, attempt_id))

    assert response.status_code == 200, response.text
    assert response.json()["disposition"] == "parked"
    _assert_both_audiences_owed(await _notice(async_client, story_id), story_id, AUDIT_MESSAGE)


@pytest.mark.parametrize(
    "audit_changes,park_changes",
    [
        ({"reason": "project_locked"}, {}),
        ({"message": "Another admission message."}, {}),
        ({"iteration": 1}, {}),
        ({}, {"detail": "A detail the audit never recorded."}),
        ({"task_id": "task-elsewhere"}, {}),
    ],
)
@pytest.mark.asyncio
async def test_a_no_run_park_mismatching_its_admission_audit_fails_closed(
    async_client: AsyncClient,
    db_session: AsyncSession,
    _tasks_project,
    audit_changes: dict,
    park_changes: dict,
) -> None:
    story_id, task_id = await _story_task(async_client)
    attempt_id = f"eng-audited-{uuid.uuid4().hex[:10]}"
    await _admission_audit(
        db_session,
        attempt_id=attempt_id,
        **{"task_id": task_id, "story_id": story_id, **audit_changes},
    )

    response = await _park_call(
        async_client, story_id, {**_audit_park(task_id, attempt_id), **park_changes}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_attempt_fence"
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)
    assert await _notice(async_client, story_id) is None


@pytest.mark.asyncio
async def test_a_no_run_park_without_any_audit_fails_closed(
    async_client: AsyncClient, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)

    response = await _park_call(async_client, story_id, _audit_park(task_id, "eng-never-decided"))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "refusal_evidence_missing"
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)
    assert await _notice(async_client, story_id) is None


@pytest.mark.asyncio
async def test_a_no_run_park_with_ambiguous_audits_fails_closed(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client)
    attempt_id = f"eng-audited-{uuid.uuid4().hex[:10]}"
    for _ in range(2):
        await _admission_audit(
            db_session, attempt_id=attempt_id, task_id=task_id, story_id=story_id
        )

    response = await _park_call(async_client, story_id, _audit_park(task_id, attempt_id))

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "refusal_evidence_ambiguous"
    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)


# --- admission parks its own refusal in the deciding transaction ---------------------


async def _publish_executors(redis: Redis, availability: ExecutorAvailability, code: str) -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=10)
    await redis.set(
        EXECUTOR_DIAGNOSTICS_REDIS_KEY,
        ExecutorDiagnosticSnapshot(
            schema_version="v1",
            version=f"infra-park-{code}-{uuid.uuid4().hex[:8]}",
            observed_at=now,
            expires_at=expiry,
            diagnostics=[
                ExecutorDiagnostic(
                    executor=executor,
                    enabled=True,
                    auth_mode=ExecutorAuthMode.HOST_SESSION,
                    availability=availability,
                    observed_at=now,
                    expires_at=expiry,
                    active_lease_count=0,
                    reason_code=code,
                    reason=safe_executor_diagnostic_reason(code),
                )
                for executor in (AgentType.CLAUDE, AgentType.CODEX)
            ],
        ).model_dump_json(),
        ex=600,
    )


@pytest.fixture
async def executors_down(redis_client: Redis):
    await _publish_executors(redis_client, ExecutorAvailability.UNAVAILABLE, "local_auth_invalid")
    yield redis_client
    await _publish_executors(redis_client, ExecutorAvailability.AVAILABLE, "ready")


async def _dispatchable_project(client: AsyncClient) -> str:
    telegram_id = uuid.uuid4().int % 1_000_000_000
    created_user = await client.post(
        "/api/users/", json={"telegram_id": telegram_id, "username": f"infra-{telegram_id}"}
    )
    assert created_user.status_code == 201, created_user.text
    project_id = str(uuid.uuid4())
    created = await client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(telegram_id)},
        json={
            "id": project_id,
            "title": "Infrastructure park admission",
            "initiating_run_id": f"init-{uuid.uuid4().hex}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
    )
    assert created.status_code == 201, created.text
    return project_id


async def _task_audits(db_session: AsyncSession, task_id: str) -> list[WorkAdmissionAudit]:
    audits = (
        await db_session.scalars(
            select(WorkAdmissionAudit).where(WorkAdmissionAudit.subject == "paid_work")
        )
    ).all()
    return [audit for audit in audits if (audit.command_payload or {}).get("task_id") == task_id]


@pytest.mark.asyncio
async def test_admission_parks_its_refusal_so_a_lost_answer_mints_nothing_more(
    async_client: AsyncClient, db_session: AsyncSession, executors_down: Redis
) -> None:
    project_id = await _dispatchable_project(async_client)
    story_id, task_id = await _story_task(async_client, project_id=project_id)

    first = await async_client.post(ADMISSION_URL, json={"task_id": task_id})

    assert first.status_code == 200, first.text
    decision = first.json()
    assert decision["reason"] == EngineeringDispatchRefusal.EXECUTOR_UNAVAILABLE
    assert decision["infrastructure_park"] == "parked"
    park = {
        "execution_phase": "pre_agent_refused",
        "refusal": "executor_unavailable",
        "task_id": task_id,
        "attempt_id": decision["run_id"],
        "detail": decision["paid_work"]["admission"]["message"],
    }
    assert await _state(async_client, story_id, task_id) == (
        "waiting_human_review",
        {KEY: park},
        0,
        "waiting_human_review",
        {KEY: park},
    )
    _assert_both_audiences_owed(await _notice(async_client, story_id), story_id, park["detail"])

    # The scheduler never saw that answer. Its next tick asks again.
    second = await async_client.post(ADMISSION_URL, json={"task_id": task_id})

    assert second.json()["reason"] == EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE
    assert second.json()["run_id"] is None
    assert [audit.reference_id for audit in await _task_audits(db_session, task_id)] == [
        decision["run_id"]
    ]
    runs = await db_session.scalars(select(Run.id).where(Run.task_id == task_id))
    assert runs.all() == []
    # The endpoint recognises the park admission already made as the same park.
    replay = await _park_call(async_client, story_id, park)
    assert replay.json()["disposition"] == "already_parked"

    await _publish_executors(executors_down, ExecutorAvailability.AVAILABLE, "ready")
    retried = await async_client.post(
        f"/api/stories/{story_id}/retry-infrastructure-attempt",
        json={
            "task_id": task_id,
            "attempt_id": decision["run_id"],
            "refusal": "executor_unavailable",
            "actor": "admin",
        },
    )
    assert retried.json()["outcome"] == "retried"
    fresh = await async_client.post(ADMISSION_URL, json={"task_id": task_id})
    assert fresh.json()["outcome"] == "admitted", fresh.text
    runs = await db_session.scalars(select(Run.id).where(Run.task_id == task_id))
    assert runs.all() == [fresh.json()["run_id"]]


@pytest.mark.asyncio
async def test_a_failure_while_admission_parks_commits_neither_refusal_nor_park(
    async_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    executors_down: Redis,
) -> None:
    project_id = await _dispatchable_project(async_client)
    story_id, task_id = await _story_task(async_client, project_id=project_id)

    def _fail_after_task_hops(*_args, **_kwargs) -> None:
        raise RuntimeError("injected failure after the task hops")

    monkeypatch.setattr("src.routers._story_helpers._do_transition", _fail_after_task_hops)
    with pytest.raises(RuntimeError, match="injected failure"):
        await async_client.post(ADMISSION_URL, json={"task_id": task_id})
    monkeypatch.undo()

    assert await _state(async_client, story_id, task_id) == ("todo", None, 0, "in_progress", None)
    assert await _task_audits(db_session, task_id) == []
    assert await _notice(async_client, story_id) is None
    assert await _park_hops(async_client, task_id) == []


@pytest.mark.asyncio
async def test_admission_parks_a_standalone_task_on_the_task_alone(
    async_client: AsyncClient, db_session: AsyncSession, executors_down: Redis
) -> None:
    project_id = await _dispatchable_project(async_client)
    created = await async_client.post(
        "/api/tasks/", json={"project_id": project_id, "title": "Standalone", "status": "todo"}
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]

    decision = (await async_client.post(ADMISSION_URL, json={"task_id": task_id})).json()

    assert decision["infrastructure_park"] == "parked"
    task = (await async_client.get(f"/api/tasks/{task_id}")).json()
    assert (task["status"], task["current_iteration"]) == ("waiting_human_review", 0)
    assert task["failure_metadata"][KEY]["attempt_id"] == decision["run_id"]
    assert await _park_hops(async_client, task_id) == ["in_dev", "waiting_human_review"]


@pytest.mark.asyncio
async def test_admission_fences_a_parked_story_and_a_parked_task_without_an_attempt(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    sibling = await async_client.post(
        "/api/tasks/",
        json={"project_id": PROJECT_ID, "story_id": story_id, "title": "Sibling", "status": "todo"},
    )
    assert sibling.status_code == 201, sibling.text
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    assert (await _park_call(async_client, story_id, park)).json()["disposition"] == "parked"
    evidence_task = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": PROJECT_ID,
            "title": "Carries a park",
            "status": "todo",
            "failure_metadata": {KEY: _run_park("elsewhere", "eng-elsewhere")},
        },
    )
    assert evidence_task.status_code == 201, evidence_task.text

    for fenced_id in (sibling.json()["id"], evidence_task.json()["id"]):
        decision = await async_client.post(ADMISSION_URL, json={"task_id": fenced_id})
        assert decision.status_code == 200, decision.text
        assert decision.json()["reason"] == EngineeringDispatchRefusal.INFRASTRUCTURE_PARKED
        assert decision.json()["run_id"] is None
        assert await _task_audits(db_session, fenced_id) == []


# --- the owed-notification selection serves each audience ---------------------------


@pytest.mark.asyncio
async def test_a_story_owing_only_its_administrators_is_selected(
    async_client: AsyncClient, db_session: AsyncSession, _tasks_project
) -> None:
    story_id, task_id = await _story_task(async_client, task_status="failed")
    park = _run_park(task_id, await _refused_run(db_session, story_id, task_id))
    assert (await _park_call(async_client, story_id, park)).json()["disposition"] == "parked"
    notice = await _notice(async_client, story_id)

    owner_delivered = await async_client.patch(
        f"/api/stories/{story_id}/owner-notification",
        json={**notice, "state": "delivered", "attempts": 1},
    )
    assert owner_delivered.status_code == 200, owner_delivered.text
    owed = await async_client.get("/api/stories/owner-notifications/owed?limit=500")
    assert owed.status_code == 200, owed.text
    selected = [row for row in owed.json() if row["id"] == story_id]

    assert len(selected) == 1
    assert selected[0]["owner_notification"]["state"] == "delivered"
    assert selected[0]["owner_notification"]["admin_state"] == "owed"

    settled = await async_client.patch(
        f"/api/stories/{story_id}/owner-notification",
        json={**notice, "state": "delivered", "attempts": 1, "admin_state": "delivered"},
    )
    assert settled.status_code == 200, settled.text
    owed = await async_client.get("/api/stories/owner-notifications/owed?limit=500")
    assert all(row["id"] != story_id for row in owed.json())
