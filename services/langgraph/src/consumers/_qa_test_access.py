"""Temporary private-bot access around one QA run."""

from __future__ import annotations

import asyncio
import uuid

from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.run_result import (
    DeployRunResult,
    QABlocker,
    QABlockerCategory,
    QATestAccessLifecycle,
)
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployOutcome, DeployTrigger
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..prompts.qa import QA_TEST_TELEGRAM_ID

# The deployer may wait 600 seconds for the first run and another 600 after a
# rerun. Access cleanup must never overtake a still-running grant.
QA_TEST_ACCESS_DEPLOY_TIMEOUT = 1260
QA_TEST_ACCESS_POLL_INTERVAL = 2
_TEST_ID_KEY = "TG_BOT_TEST_TELEGRAM_ID"


class QAAccessDeployCancelled(asyncio.CancelledError):
    """Cancellation carrying the child deploy that must be revoked."""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id


def needs_temporary_qa_access(project: ProjectDTO) -> bool:
    """Only private bot projects receive the platform's temporary identity."""
    access = (project.config or {}).get("bot_access")
    return isinstance(access, dict) and access.get("mode") in {"only_me", "custom"}


def _blocker(*, attempted: str, received: str) -> QABlocker:
    return QABlocker(
        category=QABlockerCategory.UNKNOWN,
        attempted=attempted,
        sent="temporary QA bot access deployment",
        received=received,
    )


async def _wait_for_deploy(run_id: str) -> tuple[bool, str]:
    """Wait for the child deploy's terminal result, never inferring success from dispatch."""
    deadline = asyncio.get_running_loop().time() + QA_TEST_ACCESS_DEPLOY_TIMEOUT
    while True:
        raw = await api_client.get(f"runs/{run_id}")
        run = RunDTO.model_validate(raw)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            succeeded = (
                run.status == RunStatus.COMPLETED
                and isinstance(run.result, DeployRunResult)
                and run.result.deploy_outcome == DeployOutcome.SUCCESS
            )
            return succeeded, run.status.value
        if asyncio.get_running_loop().time() >= deadline:
            return False, "timed out waiting for terminal deploy result"
        await asyncio.sleep(QA_TEST_ACCESS_POLL_INTERVAL)


async def _wait_for_project_deploy_slot(redis: RedisStreamClient, project_id: str) -> bool:
    """Do not enqueue revocation until the grant worker has released its lock."""
    deadline = asyncio.get_running_loop().time() + QA_TEST_ACCESS_DEPLOY_TIMEOUT
    lock_key = f"deploy:{project_id}:lock"
    while await redis.redis.exists(lock_key):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(QA_TEST_ACCESS_POLL_INTERVAL)
    return True


async def _dispatch_deploy(
    *,
    parent_run_id: str,
    project_id: str,
    application_id: int,
    head_sha: str,
    phase: str,
    env_overrides: dict[str, str],
    redis: RedisStreamClient,
) -> tuple[str, bool, str]:
    run_id = f"qa-access-{phase}-{uuid.uuid4().hex[:8]}"
    await api_client.post(
        "runs/",
        json={
            "id": run_id,
            "type": RunType.DEPLOY.value,
            "project_id": project_id,
            "status": RunStatus.QUEUED.value,
            "run_metadata": {
                "qa_parent_run_id": parent_run_id,
                "qa_test_access_phase": phase,
                "application_id": application_id,
                "head_sha": head_sha,
            },
        },
    )
    await redis.publish_message(
        DEPLOY_QUEUE,
        DeployMessage(
            task_id=run_id,
            project_id=project_id,
            triggered_by=DeployTrigger.ADMIN,
            action=DeployAction.FEATURE,
            head_sha=head_sha,
            env_overrides=env_overrides,
        ),
    )
    try:
        succeeded, detail = await _wait_for_deploy(run_id)
    except asyncio.CancelledError as exc:
        raise QAAccessDeployCancelled(run_id) from exc
    return run_id, succeeded, detail


async def grant_temporary_qa_access(
    *,
    parent_run_id: str,
    project_id: str,
    application_id: int,
    head_sha: str,
    redis: RedisStreamClient,
) -> tuple[QATestAccessLifecycle, QABlocker | None]:
    """Deploy the QA identity and wait until that deployment has finished."""
    lifecycle = QATestAccessLifecycle(in_test_mode=False, grant_succeeded=False)
    try:
        run_id, succeeded, detail = await _dispatch_deploy(
            parent_run_id=parent_run_id,
            project_id=project_id,
            application_id=application_id,
            head_sha=head_sha,
            phase="grant",
            env_overrides={_TEST_ID_KEY: str(QA_TEST_TELEGRAM_ID)},
            redis=redis,
        )
    except Exception as exc:
        return lifecycle, _blocker(attempted="grant temporary QA bot access", received=str(exc))
    lifecycle = QATestAccessLifecycle(
        in_test_mode=succeeded,
        grant_run_id=run_id,
        grant_succeeded=succeeded,
    )
    if succeeded:
        try:
            await api_client.patch(
                f"runs/{parent_run_id}",
                json={"run_metadata": {"in_test_mode": True, "test_access_grant_run_id": run_id}},
            )
        except Exception as exc:
            return lifecycle, _blocker(
                attempted="record temporary QA bot access grant",
                received=str(exc),
            )
        return lifecycle, None
    return lifecycle, _blocker(
        attempted="grant temporary QA bot access",
        received=f"grant deployment {run_id} did not succeed: {detail}",
    )


async def revoke_temporary_qa_access(
    *,
    lifecycle: QATestAccessLifecycle,
    parent_run_id: str,
    project_id: str,
    application_id: int,
    head_sha: str,
    redis: RedisStreamClient,
) -> tuple[QATestAccessLifecycle, QABlocker | None]:
    """Remove the override and wait for its deployment on every QA outcome."""
    try:
        if lifecycle.grant_run_id:
            grant_succeeded, grant_detail = await _wait_for_deploy(lifecycle.grant_run_id)
            if not grant_succeeded and grant_detail.startswith("timed out"):
                return lifecycle.model_copy(update={"revoke_succeeded": False}), _blocker(
                    attempted="wait for grant before revoking temporary QA bot access",
                    received=grant_detail,
                )
            if not await _wait_for_project_deploy_slot(redis, project_id):
                return lifecycle.model_copy(update={"revoke_succeeded": False}), _blocker(
                    attempted=(
                        "wait for grant deployment lock before revoking temporary QA bot access"
                    ),
                    received="timed out waiting for grant deploy lock to release",
                )
        run_id, succeeded, detail = await _dispatch_deploy(
            parent_run_id=parent_run_id,
            project_id=project_id,
            application_id=application_id,
            head_sha=head_sha,
            phase="revoke",
            env_overrides={},
            redis=redis,
        )
    except Exception as exc:
        lifecycle = lifecycle.model_copy(update={"revoke_succeeded": False})
        return lifecycle, _blocker(attempted="revoke temporary QA bot access", received=str(exc))

    lifecycle = lifecycle.model_copy(
        update={
            "in_test_mode": not succeeded,
            "revoke_run_id": run_id,
            "revoke_succeeded": succeeded,
        }
    )
    await api_client.patch(
        f"runs/{parent_run_id}",
        json={
            "run_metadata": {
                "in_test_mode": not succeeded,
                "test_access_revoke_run_id": run_id,
            }
        },
    )
    if succeeded:
        return lifecycle, None
    return lifecycle, _blocker(
        attempted="revoke temporary QA bot access",
        received=f"revocation deployment {run_id} did not succeed: {detail}",
    )
