"""Internal/admin agent-free publication and locked execution authority."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
import httpx
from sqlalchemy import select

from shared.contracts.dto.commit_publication import (
    COMMIT_PUBLICATION_KEY,
    AttemptDisposition,
    CommitPublication,
    CommitRecoveryCommand,
    CommitRecoveryRead,
    EngineeringStop,
    PublicationFailure,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.worker import CreateWorkerCommand, DeleteWorkerCommand
from shared.contracts.queues.worker_result import WorkerFailedResult
from shared.contracts.worker_turn import EngineeringTurnPublication
from shared.models import Repository, Run, WorkAdmissionAudit
from shared.models.commit_recovery import CommitRecovery
from shared.queues import WORKER_COMMANDS
from shared.redis.client import DEFAULT_STREAM_MAXLEN

from ..attempt_disposition import lock_story_attempts, locked_disposition
from ..database import get_async_session
from ..dependencies import get_internal_or_admin_actor, get_redis_client, require_internal_or_admin
from ..schemas.story import StoryRead
from ._story_helpers import _do_transition

router = APIRouter(tags=["commit recovery"])


@router.get("/engineering-stops/pending", response_model=list[StoryRead])
async def pending_engineering_stops(
    db=Depends(get_async_session), _=Depends(require_internal_or_admin)
):
    from shared.models import Story

    return list(
        (
            await db.scalars(
                select(Story).where(
                    Story.engineering_stop["id"].as_string().is_not(None),
                    Story.engineering_stop["released_at"].as_string().is_(None),
                )
            )
        ).all()
    )


_PUBLISH_TURN = """
local saved = redis.call('GET', KEYS[2])
if saved then return saved end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*', 'data', ARGV[1])
redis.call('SET', KEYS[2], id)
return id
"""


@router.post("/runs/{attempt_id}/publish-worker-turn")
async def publish_turn(
    attempt_id: str,
    command: EngineeringTurnPublication,
    db=Depends(get_async_session),
    redis=Depends(get_redis_client),
    _=Depends(require_internal_or_admin),
):
    from shared.queues import worker_input_stream
    from shared.redis import decode_redis_fields

    story, task, project, run, runs = await lock_attempt(attempt_id, db)
    authority = await locked_disposition(story, task, runs, db)
    if authority is not AttemptDisposition.ELIGIBLE or run.status != RunStatus.RUNNING.value:
        refuse("engineering_attempt_fenced", "This attempt cannot publish another worker turn")
    if command.turn.attempt_id != run.id or command.worker_id != (run.run_metadata or {}).get(
        "worker_id"
    ):
        refuse("ownership_missing", "Turn differs from the persisted worker/attempt identity")
    meta = decode_redis_fields(await redis.redis.hgetall(f"worker:meta:{command.worker_id}"))
    if meta.get("project_id") != str(project.id) or meta.get("story_id") != run.story_id:
        refuse("ownership_missing", "Worker no longer owns this attempt's checkout")
    # The API holds the Story fence through XADD. Native Redis atomically owns
    # the request identity and stream entry, even when the HTTP answer is lost.
    stream_id = await redis.redis.eval(
        _PUBLISH_TURN,
        2,
        worker_input_stream(command.worker_id),
        f"engineering:turn-publication:{run.id}:{command.turn.request_id}",
        command.turn.model_dump_json(exclude_none=True),
        DEFAULT_STREAM_MAXLEN,
    )
    return {"stream_id": stream_id}


@router.post("/runs/{attempt_id}/publish-worker-command")
async def publish_worker_command(
    attempt_id: str,
    command: CreateWorkerCommand,
    db=Depends(get_async_session),
    redis=Depends(get_redis_client),
    _=Depends(require_internal_or_admin),
):
    story, task, project, run, runs = await lock_attempt(attempt_id, db)
    authority = await locked_disposition(story, task, runs, db)
    if authority is not AttemptDisposition.ELIGIBLE or COMMIT_PUBLICATION_KEY in (
        project.config or {}
    ):
        refuse("engineering_attempt_fenced", "This attempt cannot create a worker")
    owner = command.config.ownership
    if (
        run.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}
        or owner.attempt_id != run.id
        or owner.project_id != str(project.id)
        or owner.story_id != run.story_id
        or owner.run_id != project.initiating_run_id
        or command.config.worker_type != "developer"
    ):
        refuse("ownership_missing", "Worker command differs from the admitted attempt")
    stream_id = await redis.redis.eval(
        _PUBLISH_TURN,
        2,
        WORKER_COMMANDS,
        f"engineering:worker-command:{run.id}:{command.request_id}",
        command.model_dump_json(),
        DEFAULT_STREAM_MAXLEN,
    )
    return {"stream_id": stream_id}


def refuse(code, message):
    raise HTTPException(409, {"code": code, "message": message})


async def lock_attempt(attempt_id, db):
    from shared.contracts.dto.run import RunType
    from shared.models import Project, Task

    edges = (
        await db.execute(
            select(Run.story_id, Run.task_id, Run.project_id, Run.type).where(Run.id == attempt_id)
        )
    ).one_or_none()
    if edges is None:
        raise HTTPException(404, "Engineering attempt not found")
    if edges.type != RunType.ENGINEERING.value:
        refuse("ownership_missing", "Run is not engineering")
    if not edges.story_id:
        task = (
            await db.scalar(select(Task).where(Task.id == edges.task_id).with_for_update())
            if edges.task_id
            else None
        )
        project = await db.scalar(
            select(Project).where(Project.id == edges.project_id).with_for_update()
        )
        run = await db.scalar(select(Run).where(Run.id == attempt_id).with_for_update())
        return None, task, project, run, [run]
    story, tasks, project, runs = await lock_story_attempts(edges.story_id, db)
    run = next((r for r in runs if r.id == attempt_id), None)
    if run is None:
        refuse("ownership_missing", "Run is not an engineering attempt of this Story")
    task = next((t for t in tasks if t.id == run.task_id), None)
    return story, task, project, run, runs


@router.post("/runs/{attempt_id}/engineering-disposition")
async def engineering_disposition(
    attempt_id: str, db=Depends(get_async_session), _=Depends(require_internal_or_admin)
):
    from shared.contracts.dto.commit_publication import AttemptDispositionRead

    story, task, project, run, runs = await lock_attempt(attempt_id, db)
    state = await locked_disposition(story, task, runs, db)
    if state is AttemptDisposition.ELIGIBLE and COMMIT_PUBLICATION_KEY in (project.config or {}):
        CommitPublication.model_validate(project.config[COMMIT_PUBLICATION_KEY])
        state = AttemptDisposition.PUBLICATION_REQUIRED
    if run.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        state = AttemptDisposition.STOPPED if state is AttemptDisposition.ELIGIBLE else state
    return AttemptDispositionRead(
        disposition=state,
        project_id=str(run.project_id),
        story_id=run.story_id,
        attempt_id=run.id,
        initiating_run_id=project.initiating_run_id,
    )


@router.post("/runs/{attempt_id}/park-publication")
async def park_worker_publication(
    attempt_id: str,
    output: WorkerFailedResult,
    db=Depends(get_async_session),
    _=Depends(require_internal_or_admin),
):
    from ..publication_park import park_publication

    story, task, _, run, _ = await lock_attempt(attempt_id, db)
    publication = output.publication
    if publication is None:
        refuse("ownership_missing", "Park requires the typed publication refusal")
    if (
        publication.published
        or publication.attempt_id != run.id
        or (publication.worker_id != (run.run_metadata or {}).get("worker_id"))
    ):
        refuse("ownership_missing", "Publication does not match the persisted worker turn")
    repository = await db.get(Repository, publication.repository_id)
    if (
        repository is None
        or repository.project_id != run.project_id
        or repository.git_url != publication.repository_url
        or (
            run.story_id is not None
            and publication.branch is not None
            and publication.branch != f"story/{run.story_id}"
        )
    ):
        refuse(
            "ownership_missing", "Publication repository/branch does not match the owned attempt"
        )
    saved = (run.run_metadata or {}).get(COMMIT_PUBLICATION_KEY)
    if saved is not None and saved != publication.model_dump(mode="json"):
        refuse("stale_attempt", "The attempt already owns different publication evidence")
    claim = await db.get(CommitRecovery, run.id)
    if claim is not None and claim.handed_off_at is not None:
        return publication
    await park_publication(story, task, run, publication, db)
    await _settle_publication_output(run, output, db)
    await db.commit()
    return publication


async def _settle_publication_output(run, output, db):
    from shared.contracts.dto.engineering import EngineeringStatus
    from shared.contracts.dto.engineering_attempt import EngineeringAttemptLedgerInput
    from shared.contracts.dto.run_result import EngineeringRunResult

    from .runs import _record_first_terminal_completion, _settle_terminal_accounting

    saved = (run.run_metadata or {}).get("publication_worker_result")
    facts = output.model_dump(mode="json")
    if saved is not None and saved != facts:
        refuse("stale_attempt", "This attempt already owns different worker output")
    run.run_metadata = {**(run.run_metadata or {}), "publication_worker_result": facts}
    # Preserve an already terminal outcome and ledger, including a stop which
    # settled first. Supplemental worker facts remain separate diagnostics.
    if run.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        return
    run.status = RunStatus.FAILED.value
    run.error_message = output.error
    run.result = EngineeringRunResult(
        engineering_status=EngineeringStatus.FAILED,
        failure_reason=output.failure_reason,
        publication=output.publication,
        worker_report=output.worker_report,
        execution=output.execution,
    ).model_dump(mode="json")
    run.transcript_path = output.transcript_path
    run.transcript_truncated = output.transcript_truncated
    evidence = (
        EngineeringAttemptLedgerInput(claude_evidence=output.claude_evidence)
        if output.claude_evidence
        else (
            EngineeringAttemptLedgerInput(factory_evidence=output.factory_evidence)
            if output.factory_evidence
            else EngineeringAttemptLedgerInput(
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                total_tokens=output.total_tokens,
            )
        )
    )
    _record_first_terminal_completion(run)
    await _settle_terminal_accounting(run, evidence, None, db)


async def _finish_stopped_run(run, db, redis, story, tasks):
    from shared.commit_publication import pending_publication
    from shared.contracts.dto.engineering import EngineeringStatus
    from shared.contracts.dto.run_result import EngineeringRunResult

    from .runs import _record_first_terminal_completion, _settle_terminal_accounting

    pending = await pending_publication(redis.redis, run.id)
    if pending is not None:
        from ..publication_park import park_publication

        task = next((t for t in tasks if t.id == run.task_id), None)
        await park_publication(story, task, run, pending.publication, db)
        await _settle_publication_output(run, pending, db)
        return

    # No provider facts are invented. A consumed output can still settle first;
    # cancellation here requires the owned worker to have disappeared.
    if COMMIT_PUBLICATION_KEY in (run.run_metadata or {}):
        return
    run.status = RunStatus.CANCELLED.value
    run.error_message = "Engineering stopped by an explicit Story stop."
    run.result = EngineeringRunResult(engineering_status=EngineeringStatus.FAILED).model_dump(
        mode="json"
    )
    _record_first_terminal_completion(run)
    await _settle_terminal_accounting(run, None, None, db)


async def reconcile_stop(story_id, db, redis):
    story, tasks, _, runs = await lock_story_attempts(story_id, db)
    if story.engineering_stop is None:
        return {"pending": 0}
    stop = EngineeringStop.model_validate(story.engineering_stop)
    if stop.released_at is not None:
        return {"pending": 0}
    pending = 0
    for run in runs:
        metadata = run.run_metadata or {}
        worker_id = metadata.get("worker_id")
        if worker_id is None:
            if run.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
                await _finish_stopped_run(run, db, redis, story, tasks)
            continue
        # Redis metadata is a second proof of the worker being stopped. A reused
        # worker may have another creator attempt but must still own this Story.
        meta = await redis.redis.hgetall(f"worker:meta:{worker_id}")
        if not meta:
            from shared.contracts.worker_evidence import (
                RemovedWorkerEvidence,
                removed_worker_evidence_key,
            )

            removed = await redis.redis.hget(
                removed_worker_evidence_key(metadata["initiating_run_id"]), worker_id
            )
            if removed:
                evidence = RemovedWorkerEvidence.model_validate_json(removed)
                if (
                    evidence.worker_id != worker_id
                    or evidence.ownership.story_id != story.id
                    or evidence.ownership.project_id != str(story.project_id)
                ):
                    refuse(
                        "ownership_missing", "Removed-worker proof differs from the stopped attempt"
                    )
                if run.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
                    await _finish_stopped_run(run, db, redis, story, tasks)
            else:
                pending += 1
            continue
        from shared.redis import decode_redis_fields

        meta = decode_redis_fields(meta)
        if meta.get("story_id") != story.id or meta.get("project_id") != str(story.project_id):
            refuse("ownership_missing", "Worker stop refused: ownership no longer matches")
        command = DeleteWorkerCommand(
            request_id=f"story-stop-{stop.id}-{run.id}",
            worker_id=worker_id,
            reason="failed",
        )
        # Failed/lost publication retains the committed stop. Subsequent ticks
        # redrive the same owned teardown; worker-manager deletion is idempotent.
        await redis.publish(WORKER_COMMANDS, command.model_dump(mode="json"))
        pending += 1
    await db.commit()
    return {"pending": pending}


@router.post("/stories/{story_id}/reconcile-engineering-stop")
async def reconcile_engineering_stop(
    story_id: str,
    db=Depends(get_async_session),
    redis=Depends(get_redis_client),
    _=Depends(require_internal_or_admin),
):
    return await reconcile_stop(story_id, db, redis)


@router.get("/commit-recoveries/{attempt_id}")
async def read_claim(
    attempt_id: str, db=Depends(get_async_session), _=Depends(require_internal_or_admin)
):
    claim = await db.get(CommitRecovery, attempt_id)
    if claim is None:
        raise HTTPException(404, "Publication recovery claim not found")
    return {
        **CommitRecoveryRead.model_validate(claim).model_dump(mode="json"),
        "identity": claim.identity,
    }


@router.get("/stories/{story_id}/recovered-commit", response_model=CommitRecoveryRead | None)
async def recovered_story_commit(
    story_id: str, db=Depends(get_async_session), _=Depends(require_internal_or_admin)
):
    story, _, _, runs = await lock_story_attempts(story_id, db)
    if story.status != StoryStatus.IN_PROGRESS.value:
        return None
    if story.engineering_stop is not None:
        if EngineeringStop.model_validate(story.engineering_stop).released_at is None:
            return None
    claim = await db.scalar(
        select(CommitRecovery)
        .where(CommitRecovery.story_id == story_id, CommitRecovery.handed_off_at.is_not(None))
        .order_by(CommitRecovery.handed_off_at.desc())
        .limit(1)
    )
    if claim is None:
        return None
    cycle = None if story.reopened_at is None else story.reopened_at.isoformat()
    attempt = next(r for r in runs if r.id == claim.attempt_id)
    if claim.identity["cycle"] != cycle or any(r.created_at > attempt.created_at for r in runs):
        return None
    return CommitRecoveryRead.model_validate(claim)


async def _lock_recovery_repository(repository_id, db):
    return await db.scalar(
        select(Repository)
        .where(Repository.id == repository_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def recovery_identity(story, task, project, run, db, *, adopt):
    from shared.contracts.worker_turn import AttemptTurnMetadata

    turn = AttemptTurnMetadata.from_run_metadata(run.run_metadata)
    if not turn.worker_id or not turn.initiating_run_id or not turn.pre_attempt_head_sha:
        refuse(
            "ownership_missing",
            "Persisted worker, initiating Run and checkout baseline are required; "
            "reconcile trusted attempt evidence before adoption",
        )
    if turn.initiating_run_id != project.initiating_run_id:
        refuse("ownership_missing", "Attempt belongs to another initiating Run")
    preserved = (run.run_metadata or {}).get(COMMIT_PUBLICATION_KEY)
    if preserved is not None and (
        not adopt or CommitPublication.model_validate(preserved).repository_id is not None
    ):
        evidence = CommitPublication.model_validate(preserved)
        repository = (
            await _lock_recovery_repository(evidence.repository_id, db)
            if evidence.repository_id
            else None
        )
        if (
            repository is None
            or repository.git_url != evidence.repository_url
            or (evidence.branch != f"story/{story.id}" and not (adopt and evidence.branch is None))
            or evidence.worker_id != turn.worker_id
        ):
            refuse("ownership_missing", "Preserved repository/branch/worker identity was replaced")
    elif task is not None and task.repository_id:
        repository = await _lock_recovery_repository(task.repository_id, db)
    else:
        repositories = list(
            (
                await db.scalars(
                    select(Repository)
                    .where(
                        Repository.project_id == project.id,
                        Repository.role == "primary",
                    )
                    .order_by(Repository.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if len(repositories) != 1:
            refuse("ownership_missing", "Attempt requires exactly one trusted primary repository")
        repository = repositories[0]
    if repository is None or repository.project_id != project.id:
        refuse("ownership_missing", "Attempt repository ownership is missing")
    # This branch is the native Story producer's contract, not caller input.
    return {
        "worker_id": turn.worker_id,
        "attempt_id": run.id,
        "story_id": story.id,
        "project_id": str(project.id),
        "initiating_run_id": project.initiating_run_id,
        "repository_id": repository.id,
        "repository_url": repository.git_url,
        "branch": f"story/{story.id}",
        "baseline": turn.pre_attempt_head_sha,
        "cycle": None if story.reopened_at is None else story.reopened_at.isoformat(),
        "iteration": None if task is None else task.current_iteration,
    }


async def _require_recovery_context(story, task, project, run, command, db):
    if run.status != RunStatus.FAILED.value:
        refuse("stale_attempt", "Recovery requires the original terminal failed attempt")
    if task is not None and (
        task.current_iteration != (run.run_metadata or {}).get("iteration")
        or task.status
        not in {
            TaskStatus.FAILED.value,
            TaskStatus.WAITING_HUMAN_REVIEW.value,
            TaskStatus.IN_DEV.value,
            TaskStatus.DONE.value,
        }
    ):
        refuse("stale_attempt", "Task iteration or disposition no longer belongs to this attempt")
    newer = await db.scalar(
        select(Run.id)
        .where(
            Run.project_id == project.id, Run.type == "engineering", Run.created_at > run.created_at
        )
        .limit(1)
    )
    if newer is not None:
        refuse("stale_attempt", "A newer engineering attempt replaced the preserved attempt")
    identity = await recovery_identity(
        story, task, project, run, db, adopt=command.adopt_preserved_commit
    )
    stop = (
        EngineeringStop.model_validate(story.engineering_stop) if story.engineering_stop else None
    )
    if stop is not None and stop.released_at is None and command.stop_id != stop.id:
        refuse(
            "engineering_stopped",
            "Name the exact current stop to authorize agent-free continuation",
        )
    if stop is None and command.stop_id is not None:
        refuse("stale_attempt", "The named stop does not belong to this Story")
    if stop is not None and command.stop_id is not None and command.stop_id != stop.id:
        refuse("stale_attempt", "A different stop replaced the authorized recovery")
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
        refuse("stale_attempt", "Recovery requires the parked Story in human review")
    refusal = (run.run_metadata or {}).get(COMMIT_PUBLICATION_KEY)
    if not command.adopt_preserved_commit:
        if refusal is None:
            refuse("ownership_missing", "Legacy work requires explicit adopt_preserved_commit")
        evidence = CommitPublication.model_validate(refusal)
        if evidence.commit_sha != command.commit_sha:
            refuse(
                "object_missing",
                "No verified matching local SHA; use deliberate adoption with trusted identity",
            )
    return identity, stop


async def _publication_receipt(claim, identity, run):
    from ..config import get_settings

    try:
        async with httpx.AsyncClient(timeout=150) as client:
            response = await client.post(
                f"{get_settings().worker_manager_url}/api/commit-recoveries/{run.id}/publish",
                headers={"X-Internal-Key": get_settings().internal_api_key},
            )
            response.raise_for_status()
            receipt = CommitPublication.model_validate(response.json())
    except (httpx.HTTPError, ValueError):
        receipt = CommitPublication(
            failure=PublicationFailure.CREDENTIAL_UNAVAILABLE, attempt_id=run.id
        )
    if receipt.published and (
        receipt.attempt_id != run.id
        or receipt.worker_id != identity["worker_id"]
        or receipt.branch != identity["branch"]
    ):
        refuse(
            "ownership_missing",
            "Publisher receipt does not prove this owned branch/worker/attempt",
        )
    if receipt.commit_sha is not None and receipt.commit_sha != claim.commit_sha:
        refuse("stale_attempt", "Publisher returned another local commit")
    return receipt


@router.post("/stories/{story_id}/recover-commit", response_model=CommitRecoveryRead)
async def recover_commit(
    story_id: str,
    command: CommitRecoveryCommand,
    db=Depends(get_async_session),
    actor: str = Depends(get_internal_or_admin_actor),
):
    story, task, project, run, runs = await lock_attempt(command.attempt_id, db)
    if story is None or story.id != story_id:
        refuse("ownership_missing", "Attempt belongs to another Story")
    claim = await db.scalar(
        select(CommitRecovery)
        .where(
            CommitRecovery.attempt_id == run.id,
        )
        .with_for_update()
    )
    if claim is not None and (
        claim.commit_sha != command.commit_sha or claim.stop_id != command.stop_id
    ):
        refuse("stale_attempt", "This attempt already owns another recovery claim")
    if claim is not None and claim.handed_off_at is not None:
        return CommitRecoveryRead.model_validate(claim)
    identity, stop = await _require_recovery_context(story, task, project, run, command, db)
    if claim is None:
        claim = CommitRecovery(
            attempt_id=run.id,
            story_id=story.id,
            commit_sha=command.commit_sha,
            stop_id=command.stop_id,
            actor=actor,
            claimed_at=datetime.now(UTC),
            identity=identity,
        )
        db.add(claim)
        db.add(
            WorkAdmissionAudit(
                subject="commit_recovery",
                outcome="claimed",
                reference_id=run.id,
                actor=actor,
                command_payload=command.model_dump(mode="json"),
                after_value=identity,
            )
        )
        await db.commit()
        # The durable claim survives response loss. Reacquire the same ladder
        # before external publication and keep it through exact proof/handoff.
        db.expire_all()
        return await recover_commit(story_id, command, db, actor)
    if claim.identity != identity:
        refuse("stale_attempt", "Recovery ownership, cycle or iteration changed after claim")
    if claim.receipt is None or not CommitPublication.model_validate(claim.receipt).published:
        receipt = await _publication_receipt(claim, identity, run)
        claim.receipt = receipt.model_dump(mode="json")
        await db.commit()
        if receipt.published:
            db.expire_all()
            return await recover_commit(story_id, command, db, actor)
        return CommitRecoveryRead.model_validate(claim)
    return await _handoff_recovery(story, task, project, run, claim, stop, actor, db)


async def _handoff_recovery(story, task, project, run, claim, stop, actor, db):
    # Reuse the native completion owner. Failed Run/accounting are untouched.
    if task is not None:
        from ._task_actions import apply_task_completion
        from ._task_helpers import create_status_event, validate_transition

        if task.status != TaskStatus.DONE.value:
            if task.status == TaskStatus.FAILED.value:
                old = task.status
                validate_transition(old, TaskStatus.WAITING_HUMAN_REVIEW)
                task.status = TaskStatus.WAITING_HUMAN_REVIEW.value
                await create_status_event(
                    task,
                    old,
                    TaskStatus.WAITING_HUMAN_REVIEW,
                    actor,
                    {"commit_recovery": run.id},
                    db,
                )
            old = task.status
            if old != TaskStatus.IN_DEV.value:
                validate_transition(old, TaskStatus.IN_DEV)
                task.status = TaskStatus.IN_DEV.value
                await create_status_event(
                    task, old, TaskStatus.IN_DEV, actor, {"commit_recovery": run.id}, db
                )
            await apply_task_completion(
                task, actor, {"commit_recovery": run.id, "commit_sha": claim.commit_sha}, db
            )
        metadata = dict(task.failure_metadata or {})
        metadata.pop(COMMIT_PUBLICATION_KEY, None)
        task.failure_metadata = metadata or None
    if stop is not None and stop.released_at is None:
        story.engineering_stop = stop.model_copy(
            update={
                "released_at": datetime.now(UTC),
                "release_actor": actor,
            }
        ).model_dump(mode="json")
    config = dict(project.config or {})
    project_hold = config.get(COMMIT_PUBLICATION_KEY)
    if project_hold is not None:
        held = CommitPublication.model_validate(project_hold)
        if held.attempt_id != run.id:
            refuse("stale_attempt", "A different preserved attempt owns this checkout")
        config.pop(COMMIT_PUBLICATION_KEY)
        project.config = config
    _do_transition(story, StoryStatus.IN_PROGRESS)
    claim.handed_off_at = datetime.now(UTC)
    db.add(
        WorkAdmissionAudit(
            subject="commit_recovery",
            outcome="handed_off",
            reference_id=run.id,
            actor=actor,
            after_value={"commit_sha": claim.commit_sha},
        )
    )
    await db.commit()
    return CommitRecoveryRead.model_validate(claim)
