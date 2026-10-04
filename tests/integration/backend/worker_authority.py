"""Persist test attempt authority without handing work to an agent consumer."""

from uuid import uuid4

from shared.contracts.dto.commit_publication import AttemptDisposition, AttemptDispositionRead
from shared.contracts.dto.deploy_dispatch import DeployRunStart
from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchCommand,
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
)
from shared.contracts.dto.run import RunStatus, RunType, RunUpdate
from shared.contracts.dto.story import StoryCreate
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.worker_turn import (
    AttemptTurnMetadata,
    EngineeringTurnPublication,
    WorkerTurnInput,
)


async def seed_worker_authority(api, seed_project, seed_task, owners) -> WorkerOwnership:
    """Admission commits a queued Run; no engineering queue or model is invoked.

    Every worker gets a separate Project/Story/Task/Run. Repository identities
    remain synthetic so the shipped credential refusal probes cannot mint.
    Readback is mandatory, including for invalid repository input tests.
    """
    project = await seed_project(
        name=f"dind-{uuid4().hex[:12]}",
        status="active",
        config={"workspace_ready": True},
        initiating_run_id=f"dind-{uuid4().hex}",
    )
    response = await api.post(
        "/api/stories/",
        json=StoryCreate(project_id=project["id"], title="No-model worker fixture").model_dump(
            mode="json"
        ),
    )
    response.raise_for_status()
    story = response.json()
    response = await api.post(f"/api/stories/{story['id']}/start", json={})
    response.raise_for_status()
    task = await seed_task(
        title="No-model worker fixture",
        project_id=project["id"],
        story_id=story["id"],
        status=TaskStatus.TODO,
    )
    response = await api.post(
        "/api/work-admission/engineering-dispatches",
        json=EngineeringDispatchCommand(task_id=task["id"]).model_dump(mode="json"),
    )
    response.raise_for_status()
    admitted = EngineeringDispatchRead.model_validate(response.json())
    assert admitted.outcome is EngineeringDispatchOutcome.ADMITTED, admitted
    assert admitted.run_id is not None
    owner = WorkerOwnership(
        project_id=project["id"],
        story_id=story["id"],
        run_id=project["initiating_run_id"],
        attempt_id=admitted.run_id,
    )
    owners.append(owner)
    assert admitted.initiating_run_id == owner.run_id
    await assert_persisted_worker_authority(api, owner, task["id"])
    return owner


async def assert_persisted_worker_authority(api, owner, task_id):
    """Shared proof for fixture admission and the native queue-propagation scenario."""
    rows = []
    for path in (
        f"projects/{owner.project_id}",
        f"tasks/{task_id}",
        f"runs/{owner.attempt_id}",
    ):
        response = await api.get(f"/api/{path}")
        response.raise_for_status()
        rows.append(response.json())
    persisted_project, persisted_task, run = rows
    assert persisted_project["id"] == owner.project_id
    assert persisted_project["initiating_run_id"] == owner.run_id
    if owner.story_id is not None:
        response = await api.get(f"/api/stories/{owner.story_id}")
        response.raise_for_status()
        persisted_story = response.json()
        assert persisted_story["id"] == owner.story_id
        assert persisted_story["project_id"] == owner.project_id
    assert persisted_task["id"] == task_id
    assert persisted_task["project_id"] == owner.project_id
    assert persisted_task["story_id"] == owner.story_id
    assert run["id"] == owner.attempt_id
    assert run["project_id"] == owner.project_id
    assert run["story_id"] == owner.story_id
    assert run["task_id"] == task_id
    assert run["type"] == RunType.ENGINEERING
    assert run["status"] in {RunStatus.QUEUED, RunStatus.RUNNING}
    response = await api.post(f"/api/runs/{owner.attempt_id}/engineering-disposition", json={})
    response.raise_for_status()
    decision = AttemptDispositionRead.model_validate(response.json())
    assert decision == AttemptDispositionRead(
        disposition=AttemptDisposition.ELIGIBLE,
        project_id=owner.project_id,
        story_id=owner.story_id,
        attempt_id=owner.attempt_id,
        initiating_run_id=owner.run_id,
    )


async def publish_worker_fixture_turn(
    api,
    owner: WorkerOwnership,
    worker_id: str,
    *,
    request_id: str,
    prompt: str,
    turn_deadline_seconds: int,
) -> tuple[WorkerTurnInput, str]:
    """After real worker readiness, start its admitted Run and publish one typed turn.

    No new admission or engineering consumer is involved. Native start refuses
    terminal/stopped work; publication checks locked eligibility and manager-owned
    Redis identity. Readback proves the supported metadata patch took effect.
    """
    turn = WorkerTurnInput(
        request_id=request_id,
        attempt_id=owner.attempt_id,
        turn_deadline_seconds=turn_deadline_seconds,
        prompt=prompt,
    )
    path = f"/api/runs/{owner.attempt_id}"
    response = await api.get(path)
    response.raise_for_status()
    run = response.json()
    assert run["id"] == owner.attempt_id
    assert run["project_id"] == owner.project_id and run["story_id"] == owner.story_id
    assert run["type"] == RunType.ENGINEERING
    assert run["status"] in {RunStatus.QUEUED, RunStatus.RUNNING}
    metadata = AttemptTurnMetadata.from_run_metadata(run["run_metadata"])
    assert metadata.worker_id in {None, worker_id}
    assert metadata.initiating_run_id in {None, owner.run_id}
    response = await api.post(f"{path}/start", json={})
    response.raise_for_status()
    started = DeployRunStart.model_validate(response.json())
    assert started.run_id == owner.attempt_id and started.started
    assert started.run_status is RunStatus.RUNNING
    response = await api.patch(
        path,
        json=RunUpdate(
            run_metadata=AttemptTurnMetadata(
                worker_id=worker_id, initiating_run_id=owner.run_id
            ).as_run_metadata()
        ).model_dump(mode="json", exclude_unset=True),
    )
    response.raise_for_status()
    await assert_persisted_worker_authority(api, owner, run["task_id"])
    response = await api.get(path)
    response.raise_for_status()
    persisted = response.json()
    assert persisted["status"] == RunStatus.RUNNING
    metadata = AttemptTurnMetadata.from_run_metadata(persisted["run_metadata"])
    assert metadata.worker_id == worker_id and metadata.initiating_run_id == owner.run_id
    response = await api.post(
        f"{path}/publish-worker-turn",
        json=EngineeringTurnPublication(worker_id=worker_id, turn=turn).model_dump(
            mode="json", exclude_none=True
        ),
    )
    response.raise_for_status()
    return turn, response.json()["stream_id"]
