"""Postgres/Redis proofs. Dispatcher CI only; no model or live product fixture."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from shared.contracts.dto.commit_publication import CommitPublication, PublicationFailure
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.queues.worker_result import WorkerFailedResult
from shared.contracts.worker_turn import EngineeringTurnPublication, WorkerTurnInput
from shared.models import Project, Repository, Run, Story, Task, User, WorkAdmissionAudit
from shared.models.commit_recovery import CommitRecovery
from shared.models.engineering_attempt_ledger import EngineeringAttemptLedger
from src.dependencies import get_redis_client

SHA = "b" * 40
BASE = "a" * 40


@pytest.fixture
async def attempt(db_session, async_client, request):
    suffix = uuid.uuid4().hex[:12]
    user = User(telegram_id=int(suffix, 16), username=f"recovery-{suffix}", is_admin=True)
    db_session.add(user)
    await db_session.flush()
    project = Project(
        id=uuid.uuid4(),
        title="Synthetic recovery",
        slug=f"recovery-{suffix}",
        owner_id=user.id,
        status="active",
        initiating_run_id=f"init-{suffix}",
        config={"workspace_ready": True},
    )
    db_session.add(project)
    await db_session.flush()
    story = Story(
        id=f"story-{suffix}",
        project_id=project.id,
        title="Preserved change",
        status="in_progress",
        waiting_on="none",
    )
    repo = Repository(
        id=f"repo-{suffix}",
        project_id=project.id,
        name="fixture",
        git_url="https://github.com/fixture/owned.git",
        role="primary",
        visibility="private",
        is_managed=True,
    )
    db_session.add_all([story, repo])
    await db_session.flush()
    task = Task(
        id=f"task-{suffix}",
        project_id=project.id,
        story_id=story.id,
        repository_id=repo.id,
        title="Produce a change",
        type="feature",
        status="in_dev",
        current_iteration=2,
        max_iterations=3,
        dispatch_admitted=True,
        created_by="fixture",
    )
    db_session.add(task)
    await db_session.flush()
    runs = []
    for iteration in range(3):
        run = Run(
            id=f"eng-{suffix}-{iteration}",
            type="engineering",
            status="running",
            project_id=project.id,
            story_id=story.id,
            task_id=task.id,
            created_at=datetime.now(UTC) - timedelta(minutes=3 - iteration),
            run_metadata={
                "iteration": iteration,
                "worker_id": f"worker-{suffix}",
                "initiating_run_id": project.initiating_run_id,
                "pre_attempt_head_sha": BASE,
            },
        )
        db_session.add(run)
        runs.append(run)
    await db_session.commit()
    for run in runs:
        if getattr(request, "param", None) == "live" and run is runs[-1]:
            continue
        response = await async_client.patch(
            f"/api/runs/{run.id}",
            json={
                "status": "failed",
                "error_message": "Synthetic ordinary failure",
                "result": {"engineering_status": "failed"},
            },
        )
        assert response.status_code == 200, response.text
    task.status = "in_dev" if getattr(request, "param", None) == "live" else "failed"
    await db_session.commit()
    return project, story, task, runs[-1], repo


@pytest.mark.parametrize("attempt", ["live"], indirect=True)
async def test_stop_inflight_fences_native_turn_command_and_late_deploy(
    attempt, async_client, db_session
):
    from shared.contracts.queues.worker import CreateWorkerCommand, WorkerConfig, WorkerOwnership

    project, story, task, run, repo = attempt
    redis = get_redis_client().redis
    worker_id = run.run_metadata["worker_id"]
    await redis.hset(
        f"worker:meta:{worker_id}",
        mapping={
            "worker_type": "developer",
            "project_id": str(project.id),
            "story_id": story.id,
            "repo_id": repo.id,
            "run_id": project.initiating_run_id,
            "attempt_id": run.id,
        },
    )
    stop_id = await stop(async_client, story)
    commands = await redis.xrange("worker:commands")
    deletes = [json.loads(fields.get(b"data", fields.get("data"))) for _, fields in commands]
    assert any(
        c.get("worker_id") == worker_id
        and c.get("request_id", "").startswith(f"story-stop-{stop_id}-")
        for c in deletes
    )
    before = await redis.xlen("worker:commands")
    command = CreateWorkerCommand(
        request_id=f"late-{run.id}",
        config=WorkerConfig(
            name="late-fixture",
            worker_type="developer",
            agent_type="claude",
            instructions="Controlled stale command fixture",
            allowed_commands=["engineering.start"],
            capabilities=["git"],
            repo_id=repo.id,
            branch=f"story/{story.id}",
            ownership=WorkerOwnership(
                project_id=str(project.id),
                run_id=project.initiating_run_id,
                attempt_id=run.id,
                story_id=story.id,
            ),
        ),
    )
    denied = await async_client.post(
        f"/api/runs/{run.id}/publish-worker-command", json=command.model_dump(mode="json")
    )
    assert denied.status_code == 409 and await redis.xlen("worker:commands") == before
    denied_turn = await async_client.post(
        f"/api/runs/{run.id}/publish-worker-turn",
        json={
            "worker_id": worker_id,
            "turn": {
                "attempt_id": run.id,
                "request_id": f"late-{run.id}",
                "turn_deadline_seconds": 60,
                "prompt": "must not reach a model",
            },
        },
    )
    assert denied_turn.status_code == 409
    assert await redis.xlen(f"worker:{worker_id}:input") == 0
    deploy = Run(
        id=f"deploy-{run.id}",
        type="deploy",
        status="queued",
        project_id=project.id,
        story_id=story.id,
    )
    db_session.add(deploy)
    await db_session.commit()
    for action in ("start", "dispatch-claim"):
        refused = await async_client.post(f"/api/runs/{deploy.id}/{action}", json={})
        assert refused.status_code == 200, refused.text
        assert not refused.json()["started" if action == "start" else "granted"]
    await db_session.refresh(deploy)
    assert deploy.status == "queued" and not deploy.run_metadata
    await db_session.refresh(task)
    assert task.current_iteration == 2


async def stop(client, story):
    response = await client.post(
        f"/api/stories/{story.id}/human-review", json={"actor": "forged-admin"}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["engineering_stop"]["actor"] == "internal_service"
    return result["engineering_stop"]["id"]


@pytest.mark.parametrize("attempt", ["live"], indirect=True)
@pytest.mark.parametrize(
    "invalid", [None, "request", "deadline", "attempt", "worker", "project", "queued"]
)
async def test_native_turn_publication_requires_owned_running_attempt_and_deduplicates(
    attempt, async_client, db_session, invalid
):
    project, story, _, run, _ = attempt
    redis = get_redis_client().redis
    worker_id = run.run_metadata["worker_id"]
    await redis.hset(
        f"worker:meta:{worker_id}",
        mapping={
            "project_id": "foreign" if invalid == "project" else str(project.id),
            "story_id": story.id,
        },
    )
    publication = EngineeringTurnPublication(
        worker_id=worker_id,
        turn=WorkerTurnInput(
            request_id=f"turn-{run.id}",
            attempt_id=run.id,
            turn_deadline_seconds=60,
            prompt="No model",
        ),
    )
    body = publication.model_dump(mode="json", exclude_none=True)
    if invalid in {"request", "deadline"}:
        del body["turn"]["request_id" if invalid == "request" else "turn_deadline_seconds"]
    elif invalid == "attempt":
        body["turn"]["attempt_id"] = "foreign"
    elif invalid == "worker":
        body["worker_id"] = "foreign"
    elif invalid == "queued":
        run.status = "queued"
        await db_session.commit()
    stream = f"worker:{worker_id}:input"
    path = f"/api/runs/{run.id}/publish-worker-turn"
    before = await redis.xlen(stream)
    response = await async_client.post(path, json=body)
    if invalid:
        assert response.status_code == (422 if invalid in {"request", "deadline"} else 409)
        assert await redis.xlen(stream) == before
        assert not await redis.exists(
            f"engineering:turn-publication:{run.id}:{publication.turn.request_id}"
        )
        return
    assert response.status_code == 200, response.text
    replay = await async_client.post(path, json=body)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert await redis.xlen(stream) == before + 1
    stream_id = response.json()["stream_id"]
    entries = await redis.xrange(stream, min=stream_id, max=stream_id)
    assert len(entries) == 1
    fields = entries[0][1]
    assert (
        WorkerTurnInput.model_validate_json(fields.get(b"data", fields.get("data")))
        == publication.turn
    )


@pytest.mark.asyncio
async def test_stopped_three_attempts_cannot_buy_a_fourth(attempt, async_client, db_session):
    project, story, task, run, _ = attempt
    await stop(async_client, story)
    before_commands = await get_redis_client().redis.xlen("worker:commands")
    retry = await async_client.post(f"/api/tasks/{task.id}/retry-failed", json={"actor": "admin"})
    assert retry.status_code == 409
    assert retry.json()["detail"]["disposition"] == "stopped"
    decision = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": task.id}
    )
    assert decision.status_code == 200
    assert decision.json()["outcome"] == "refused"
    # Deploy fixes have no Task. The paid gate still holds the same Story fence.
    paid = await async_client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": f"fourth-{run.id}",
            "type": "engineering",
            "project_id": str(project.id),
            "story_id": story.id,
        },
    )
    assert paid.status_code == 200
    assert paid.json()["admission"]["reason"] == "engineering_stopped"
    await db_session.refresh(task)
    await db_session.refresh(story)
    await db_session.refresh(run)
    current = await db_session.get(Task, task.id)
    assert current.current_iteration == 2 and current.status == "failed"
    runs = list((await db_session.scalars(select(Run).where(Run.story_id == story.id))).all())
    ledger = list(
        (
            await db_session.scalars(
                select(EngineeringAttemptLedger).where(
                    EngineeringAttemptLedger.story_id == story.id
                )
            )
        ).all()
    )
    assert len(runs) == len(ledger) == 3
    assert await get_redis_client().redis.xlen("worker:commands") == before_commands
    audits = list(
        (
            await db_session.scalars(
                select(WorkAdmissionAudit).where(
                    WorkAdmissionAudit.reference_id == story.id,
                    WorkAdmissionAudit.subject == "engineering_stop",
                )
            )
        ).all()
    )
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_stop_races_retry_and_fences_late_completion(attempt, async_client, db_session):
    _, story, task, run, _ = attempt
    stopped, retry = await asyncio.gather(
        async_client.post(f"/api/stories/{story.id}/human-review", json={}),
        async_client.post(f"/api/tasks/{task.id}/retry-failed", json={}),
    )
    assert stopped.status_code == 200
    assert retry.status_code in {200, 409}
    # Retry may linearize first; either ordering fences every subsequent start.
    dispatched = await async_client.post(
        "/api/work-admission/engineering-dispatches", json={"task_id": task.id}
    )
    assert dispatched.json()["outcome"] == "refused"
    completed = await async_client.post(f"/api/tasks/{task.id}/complete", json={})
    assert completed.status_code == 409
    await db_session.refresh(task)
    await db_session.refresh(story)
    await db_session.refresh(run)
    persisted = await db_session.get(Story, story.id)
    assert persisted.status == "waiting_human_review"
    assert len((await db_session.scalars(select(Run).where(Run.story_id == story.id))).all()) == 3


@pytest.mark.asyncio
async def test_publication_refusal_parks_atomically_and_preserves_paid_outcome(  # noqa: PLR0915
    attempt, async_client, db_session, monkeypatch
):
    project, story, task, old, repo = attempt
    # Use a new live attempt rather than rewrite the immutable failed fixture.
    run = Run(
        id=f"publication-{old.id}",
        type="engineering",
        status="running",
        project_id=project.id,
        story_id=story.id,
        task_id=task.id,
        run_metadata={**old.run_metadata, "iteration": 2},
    )
    db_session.add(run)
    task.status = "in_dev"
    await db_session.commit()
    evidence = CommitPublication(
        failure=PublicationFailure.PUSH_REFUSED,
        commit_sha=SHA,
        branch=f"story/{story.id}",
        attempt_id=run.id,
        worker_id=run.run_metadata["worker_id"],
        repository_id=repo.id,
        repository_url=repo.git_url,
        stderr="non-fast-forward",
    )
    result = EngineeringRunResult(
        engineering_status=EngineeringStatus.FAILED,
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=evidence,
    ).model_dump(mode="json")
    output = WorkerFailedResult(
        error="Publication refused",
        input_tokens=12,
        output_tokens=8,
        total_tokens=20,
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=evidence,
    )
    response = await async_client.post(
        f"/api/runs/{run.id}/park-publication", json=output.model_dump(mode="json")
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    await db_session.refresh(story)
    await db_session.refresh(run)
    current_task = await db_session.get(Task, task.id)
    current_story = await db_session.get(Story, story.id)
    current_run = await db_session.get(Run, run.id)
    assert current_task.status == current_story.status == "waiting_human_review"
    assert current_task.current_iteration == 2
    assert current_run.status == "failed" and current_run.result == result
    ledger = (
        await db_session.scalars(
            select(EngineeringAttemptLedger).where(
                EngineeringAttemptLedger.idempotency_key == f"engineering-run:{run.id}"
            )
        )
    ).one()
    assert ledger.total_tokens == 20 and ledger.cost_source == "unknown"
    replay = await async_client.post(
        f"/api/runs/{run.id}/park-publication", json=output.model_dump(mode="json")
    )
    assert replay.status_code == 200 and replay.json() == response.json()
    assert current_story.quarantine_reason["commit_publication"]["commit_sha"] == SHA
    assert (
        await async_client.post(f"/api/tasks/{task.id}/retry-failed", json={})
    ).status_code == 409
    # Primary typed park, not legacy adoption. Generic projections grant no
    # authority to erase the active control, including a nested replacement.
    parked_reason = (await async_client.get(f"/api/stories/{story.id}")).json()["quarantine_reason"]
    for reason in (None, {}, {"failure": {"commit_publication": evidence.model_dump(mode="json")}}):
        refused = await async_client.patch(
            f"/api/stories/{story.id}", json={"quarantine_reason": reason}
        )
        assert refused.status_code == 409, refused.text
        assert (await async_client.get(f"/api/stories/{story.id}")).json()[
            "quarantine_reason"
        ] == parked_reason
    stop_id = await stop(async_client, story)
    original_run = (await async_client.get(f"/api/runs/{run.id}")).json()
    original_ledger = ledger.to_dict()
    before_commands = await get_redis_client().redis.xlen("worker:commands")
    calls = []
    from src.routers import commit_recovery

    class Publisher:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append(url)
            receipt = CommitPublication(
                published=True,
                commit_sha=SHA,
                remote_sha=SHA,
                branch=f"story/{story.id}",
                attempt_id=run.id,
                worker_id=run.run_metadata["worker_id"],
            )
            return httpx.Response(
                200, json=receipt.model_dump(mode="json"), request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(commit_recovery.httpx, "AsyncClient", Publisher)
    command = {"attempt_id": run.id, "commit_sha": SHA, "stop_id": stop_id}
    for wrong in ({**command, "commit_sha": "c" * 40}, {**command, "stop_id": "stale-stop"}):
        refused = await async_client.post(f"/api/stories/{story.id}/recover-commit", json=wrong)
        assert refused.status_code == 409, refused.text
    answers = await asyncio.gather(
        *[
            async_client.post(f"/api/stories/{story.id}/recover-commit", json=command)
            for _ in range(2)
        ]
    )
    for answer in answers:
        assert answer.status_code == 200 and answer.json()["handed_off_at"], answer.text
    # Read/replay is the same continuation after a lost caller response.
    replay = await async_client.post(f"/api/stories/{story.id}/recover-commit", json=command)
    assert replay.json() == answers[0].json() == answers[1].json()
    assert len(calls) == 1
    await db_session.refresh(task)
    await db_session.refresh(project)
    assert task.status == "done" and task.current_iteration == 2
    assert "commit_publication" not in (task.failure_metadata or {})
    assert "commit_publication" not in project.config
    handed_off = (await async_client.get(f"/api/stories/{story.id}")).json()
    assert handed_off["status"] == "in_progress"
    assert "commit_publication" not in str(handed_off["quarantine_reason"])
    for _ in range(2):
        discovered = await async_client.get(f"/api/stories/{story.id}/recovered-commit")
        assert discovered.json()["attempt_id"] == run.id
    handoffs = list(
        (
            await db_session.scalars(
                select(WorkAdmissionAudit).where(
                    WorkAdmissionAudit.subject == "commit_recovery",
                    WorkAdmissionAudit.reference_id == run.id,
                    WorkAdmissionAudit.outcome == "handed_off",
                )
            )
        ).all()
    )
    assert len(handoffs) == 1
    late_park = await async_client.post(
        f"/api/runs/{run.id}/park-publication", json=output.model_dump(mode="json")
    )
    assert late_park.status_code == 200
    # Both typed failure surfaces reject the original publication replay before
    # creating a newer stop, changing status, or owing a new owner notification.
    for action in ("human-review", "fail"):
        late_failure = await async_client.post(
            f"/api/stories/{story.id}/{action}", json={"failure": parked_reason}
        )
        assert late_failure.status_code == 409, late_failure.text
    assert (await async_client.get(f"/api/stories/{story.id}")).json() == handed_off
    later = await async_client.post(
        f"/api/stories/{story.id}/human-review",
        json={
            "failure": {
                "code": "no_new_commit",
                "source": "engineering",
                "detail": "Later ordinary failure",
            }
        },
    )
    assert later.status_code == 200, later.text
    later_stop = later.json()["engineering_stop"]["id"]
    assert later_stop != stop_id and "commit_publication" not in str(
        later.json()["quarantine_reason"]
    )
    old_recovery = await async_client.post(f"/api/stories/{story.id}/recover-commit", json=command)
    assert old_recovery.json() == replay.json()
    assert (await async_client.get(f"/api/stories/{story.id}")).json()["engineering_stop"][
        "id"
    ] == later_stop
    # Native address-less acceptance is authorized for a Story stopped before
    # QA; it releases exactly the later reviewed stop and does not spend work.
    accepted = await async_client.post(
        f"/api/stories/{story.id}/accept-result",
        json={"basis": "Reviewed later ordinary failure"},
        headers={"X-Admin-Console-Operator": "recovery-fixture"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "completed"
    assert accepted.json()["engineering_stop"]["id"] == later_stop
    assert accepted.json()["engineering_stop"]["released_at"]
    assert (await async_client.get(f"/api/runs/{run.id}")).json() == original_run
    await db_session.refresh(ledger)
    assert ledger.to_dict() == original_ledger
    assert original_run["result"]["publication"]["stderr"] == "non-fast-forward"
    assert await get_redis_client().redis.xlen("worker:commands") == before_commands


@pytest.mark.parametrize("attempt", ["live"], indirect=True)
@pytest.mark.parametrize("newer_hold", [False, True])
async def test_primary_park_recovery_cannot_overwrite_newer_attempt_or_hold(
    attempt, async_client, db_session, newer_hold
):
    project, story, task, run, repo = attempt
    evidence = CommitPublication(
        failure=PublicationFailure.PUSH_REFUSED,
        commit_sha=SHA,
        branch=f"story/{story.id}",
        attempt_id=run.id,
        worker_id=run.run_metadata["worker_id"],
        repository_id=repo.id,
        repository_url=repo.git_url,
        stderr="original preserved diagnostics",
    )
    output = WorkerFailedResult(
        error="Publication refused",
        failure_reason=EngineeringFailureReason.WORKER_COMMIT_NOT_PUBLISHED,
        publication=evidence,
    )
    parked = await async_client.post(
        f"/api/runs/{run.id}/park-publication", json=output.model_dump(mode="json")
    )
    assert parked.status_code == 200, parked.text
    stop_id = await stop(async_client, story)
    original = (await async_client.get(f"/api/runs/{run.id}")).json()
    # Fixture persists another Story's later attempt sharing the Project checkout.
    sibling = Story(
        id=f"sibling-{story.id}",
        project_id=project.id,
        title="Later checkout owner",
        status="in_progress",
        waiting_on="none",
    )
    db_session.add(sibling)
    await db_session.flush()
    later = Run(
        id=f"later-{run.id}",
        type="engineering",
        status="failed",
        project_id=project.id,
        story_id=sibling.id,
        created_at=datetime.now(UTC) + timedelta(seconds=1),
        run_metadata={**run.run_metadata, "worker_id": "later-owned-worker"},
        result={"engineering_status": "failed"},
    )
    db_session.add(later)
    await db_session.refresh(project)
    if newer_hold:
        project.config = {
            **project.config,
            "commit_publication": evidence.model_copy(
                update={
                    "attempt_id": later.id,
                    "worker_id": "later-owned-worker",
                    "branch": f"story/{sibling.id}",
                }
            ).model_dump(mode="json"),
        }
    await db_session.commit()
    held = dict(project.config)
    refused = await async_client.post(
        f"/api/stories/{story.id}/recover-commit",
        json={"attempt_id": run.id, "commit_sha": SHA, "stop_id": stop_id},
    )
    assert refused.status_code == 409, refused.text
    assert (await async_client.get(f"/api/runs/{run.id}")).json() == original
    await db_session.refresh(project)
    await db_session.refresh(story)
    await db_session.refresh(task)
    assert project.config == held
    assert story.engineering_stop["id"] == stop_id and story.engineering_stop["released_at"] is None
    assert task.status == "waiting_human_review" and task.current_iteration == 2
    assert await db_session.get(CommitRecovery, run.id) is None


@pytest.mark.asyncio
async def test_retained_taskless_outcome_settles_exact_accounting_after_stop(
    attempt, async_client, db_session
):
    from shared.contracts.dto.run import EMPTY_RESULT_TERMINAL_KEY, EmptyEngineeringTerminal

    project, story, _, old, _ = attempt
    run = Run(
        id=f"empty-{old.id}",
        type="engineering",
        status="running",
        project_id=project.id,
        story_id=story.id,
        run_metadata={"initiating_run_id": project.initiating_run_id},
    )
    db_session.add(run)
    await db_session.commit()
    terminal = EmptyEngineeringTerminal.model_validate(
        {
            "status": "failed",
            "error_message": "Worker produced no new commit",
            "result": {
                "engineering_status": "failed",
                "failure_reason": "no_new_commit",
                "worker_report": "Controlled paid output",
            },
            "engineering_attempt": {
                "provider": "openai",
                "model": "fixture",
                "input_tokens": 17,
                "output_tokens": 3,
                "total_tokens": 20,
            },
            "transcript_path": f"/transcripts/{run.id}.jsonl",
        }
    ).model_dump(mode="json", exclude_unset=True)
    retained = await async_client.patch(
        f"/api/runs/{run.id}",
        json={
            "result": terminal["result"],
            "error_message": terminal["error_message"],
            "run_metadata": {EMPTY_RESULT_TERMINAL_KEY: terminal},
        },
    )
    assert retained.status_code == 200, retained.text
    stop_id = await stop(async_client, story)
    before = (await async_client.get(f"/api/runs/{run.id}")).json()
    assert before["status"] == "running"
    assert before["result"] == EngineeringRunResult.model_validate(terminal["result"]).model_dump(
        mode="json"
    )
    for stale in (
        {"result": None},
        {"error_message": None},
        {"transcript_path": None},
        {"status": "failed"},
        {**terminal, "engineering_attempt": None},
    ):
        refused = await async_client.patch(f"/api/runs/{run.id}", json=stale)
        assert refused.status_code == 409, refused.text
        assert (await async_client.get(f"/api/runs/{run.id}")).json() == before
    # Compact and expanded JSON carry the same typed outcome. Both replays
    # preserve the canonical persisted answer and its one ledger fact.
    for payload in (
        terminal,
        {**terminal, "result": before["result"]},
        terminal,
    ):
        settled = await async_client.patch(f"/api/runs/{run.id}", json=payload)
        assert settled.status_code == 200, settled.text
    await db_session.refresh(story)
    assert story.engineering_stop["id"] == stop_id
    assert story.engineering_stop["released_at"] is None
    ledger = (
        await db_session.scalars(
            select(EngineeringAttemptLedger).where(EngineeringAttemptLedger.run_id == run.id)
        )
    ).one()
    assert ledger.provider == "openai" and ledger.total_tokens == 20
    assert settled.json()["transcript_path"] == terminal["transcript_path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("taskless", [False, True])
async def test_explicit_legacy_adoption_concurrent_replay_keeps_terminal_ledger(
    attempt, async_client, db_session, monkeypatch, taskless
):
    _, story, task, run, _ = attempt
    stop_id = await stop(async_client, story)
    # Legacy failure has no publication fields, but complete trusted identity.
    task.status = "waiting_human_review"
    if taskless:
        run.task_id = None
        task.status = "done"
    await db_session.commit()
    original = (await async_client.get(f"/api/runs/{run.id}")).json()
    original_ledger = (
        (
            await db_session.scalars(
                select(EngineeringAttemptLedger).where(
                    EngineeringAttemptLedger.idempotency_key == f"engineering-run:{run.id}"
                )
            )
        )
        .one()
        .to_dict()
    )
    calls = []

    async def publish(self, url, **kwargs):
        assert url.endswith(f"/api/commit-recoveries/{run.id}/publish")
        # Controlled worker-manager execution boundary; native Git has separate
        # offline proofs. No executor, Docker, token service or model is called.
        calls.append(url)
        receipt = CommitPublication(
            published=True,
            commit_sha=SHA,
            remote_sha=SHA,
            branch=f"story/{story.id}",
            attempt_id=run.id,
            worker_id=run.run_metadata["worker_id"],
        )
        return httpx.Response(
            200, json=receipt.model_dump(mode="json"), request=httpx.Request("POST", url)
        )

    # Patch only the recovery execution HTTP boundary, not ASGI client requests.
    from src.routers import commit_recovery

    class Publisher:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        post = publish

    monkeypatch.setattr(commit_recovery.httpx, "AsyncClient", Publisher)
    command = {
        "attempt_id": run.id,
        "commit_sha": SHA,
        "adopt_preserved_commit": True,
        "stop_id": stop_id,
    }
    responses = await asyncio.gather(
        *[
            async_client.post(f"/api/stories/{story.id}/recover-commit", json=command)
            for _ in range(2)
        ]
    )
    for response in responses:
        assert response.status_code == 200, response.text
        assert response.json()["handed_off_at"]
    # A caller that lost either response replays the same persisted result.
    replay = await async_client.post(f"/api/stories/{story.id}/recover-commit", json=command)
    assert replay.json() == responses[0].json() == responses[1].json()
    assert len(calls) == 1
    await db_session.refresh(task)
    await db_session.refresh(story)
    await db_session.refresh(run)
    assert (await db_session.get(Task, task.id)).status == "done"
    assert (await db_session.get(Story, story.id)).status == "in_progress"
    discoverable = await async_client.get(f"/api/stories/{story.id}/recovered-commit")
    assert discoverable.status_code == 200
    assert discoverable.json()["receipt"]["remote_sha"] == SHA
    unchanged = (await async_client.get(f"/api/runs/{run.id}")).json()
    assert unchanged == original
    ledger = (
        (
            await db_session.scalars(
                select(EngineeringAttemptLedger).where(
                    EngineeringAttemptLedger.idempotency_key == f"engineering-run:{run.id}"
                )
            )
        )
        .one()
        .to_dict()
    )
    assert ledger == original_ledger
    assert (
        len(
            (
                await db_session.scalars(
                    select(CommitRecovery).where(CommitRecovery.story_id == story.id)
                )
            ).all()
        )
        == 1
    )
    assert len((await db_session.scalars(select(Run).where(Run.story_id == story.id))).all()) == 3
