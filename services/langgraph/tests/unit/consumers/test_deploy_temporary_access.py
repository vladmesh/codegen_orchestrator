"""A temporary QA grant or revoke calls the target's users-core capability and deploys nothing.

These drive `process_deploy_job` with the scheduler's operation message against the
fake boundaries of `tests.unit.temporary_access_doubles`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.temporary_access import TemporaryAccessStatus
from shared.contracts.dto.users_grant import USERS_GRANT_INTENT_KEY
from shared.contracts.queues.deploy import DeployAction, DeployOutcome
from src.deploy_fence import DeployFence, DeployWrite
from tests.unit.temporary_access_doubles import (
    CAPABILITY,
    HEAD_SHA,
    NOW,
    OTHER_APPLICATION_ID,
    PROJECT_ID,
    QA_IDENTITY,
    TARGET_APPLICATION_ID,
    TARGET_URL,
    TASK_ID,
    access_grant,
    assert_credential_safe,
    assert_deployed_nothing,
    assert_retryable_failure,
    dispatched_grant_run,
    operation_env,
    process_operation,
    proves_operation,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "effect_path"), [("grant", "/users/grant"), ("revoke", "/users/revoke")]
)
async def test_operation_calls_only_the_targets_capability_and_readback(operation, effect_path):
    env = operation_env(operation)

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.product.base_urls == [TARGET_URL]
    assert env.product.calls() == [("POST", effect_path), ("GET", "/users/access")]
    effect, readback = env.product.requests
    assert effect.headers["X-Grant-Capability"] == CAPABILITY
    assert json.loads(effect.content) == QA_IDENTITY
    assert dict(readback.url.params) == QA_IDENTITY
    assert "X-Grant-Capability" not in readback.headers
    assert env.product.active is (operation == "grant")
    [write] = env.api.run_writes
    assert proves_operation(write)
    assert write["result"]["application_id"] == TARGET_APPLICATION_ID
    assert env.api.allocation_reads == [TARGET_APPLICATION_ID]
    assert_deployed_nothing(env)
    assert_credential_safe(env)
    assert env.stream.redis.value is None


@pytest.mark.asyncio
async def test_grant_acts_on_the_recorded_target_not_the_repositorys_other_application():
    env = operation_env("grant")
    env.api.grant = env.api.current_grant = access_grant(
        "grant", target_application_id=OTHER_APPLICATION_ID, target_base_url="https://other.test"
    )

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.api.allocation_reads == [OTHER_APPLICATION_ID]
    assert env.product.base_urls == ["https://other.test"]
    assert env.api.run_writes[0]["result"]["application_id"] == OTHER_APPLICATION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "current"),
    [
        pytest.param(
            "grant",
            {"status": TemporaryAccessStatus.REVOKING, "revoke_run_id": "revoke-2"},
            id="grant-after-cleanup-started",
        ),
        pytest.param("grant", {"grant_run_id": "temporary-access-grant-9"}, id="grant-replaced"),
        pytest.param(
            "revoke",
            {"status": TemporaryAccessStatus.REVOKED, "revoked_at": NOW},
            id="revoke-already-settled",
        ),
    ],
)
async def test_a_superseded_operation_never_reaches_the_product(operation, current):
    env = operation_env(operation)
    env.api.current_grant = access_grant(operation, **current)

    await process_operation(env)

    assert env.api.grant_reads == 2
    assert env.product.requests == []
    assert_retryable_failure(env, "temporary_access_operation_superseded")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current",
    [
        {"target_application_id": OTHER_APPLICATION_ID},
        {"head_sha": "c" * 40},
        {"external_id": "1"},
    ],
    ids=["target", "head-sha", "identity"],
)
async def test_a_record_that_no_longer_matches_never_reaches_the_product(current):
    env = operation_env("grant")
    env.api.current_grant = access_grant("grant", **current)

    await process_operation(env)

    assert env.product.requests == []
    assert_retryable_failure(env, "temporary_access_target_mismatch")


def _lose_lock_at(monkeypatch, env, write: DeployWrite, occurrence: int) -> None:
    check = DeployFence.ensure_held
    seen = []

    async def ensure_held(fence: DeployFence, checked: DeployWrite) -> None:
        if checked is write:
            seen.append(checked)
            if len(seen) == occurrence:
                env.stream.redis.value = "another-deploy"
        await check(fence, checked)

    monkeypatch.setattr(DeployFence, "ensure_held", ensure_held)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_a_lock_lost_before_the_effect_makes_no_product_call(monkeypatch, operation):
    env = operation_env(operation)
    _lose_lock_at(monkeypatch, env, DeployWrite.PRODUCT_ACCESS, 1)

    result = await process_operation(env)

    assert env.product.requests == []
    assert result["reason"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    [write] = env.api.run_writes
    assert write["result"]["deploy_outcome"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    assert env.stream.redis.value == "another-deploy"


@pytest.mark.asyncio
async def test_a_lock_lost_before_the_terminal_write_never_records_proof(monkeypatch):
    env = operation_env("grant")
    # The first RUN_STATE check is the claim's own start of the Run.
    _lose_lock_at(monkeypatch, env, DeployWrite.RUN_STATE, 2)

    result = await process_operation(env)

    assert env.product.calls() == [("POST", "/users/grant"), ("GET", "/users/access")]
    assert result["reason"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    [write] = env.api.run_writes
    assert write["result"]["deploy_outcome"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    assert not proves_operation(write)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secrets", "reason"),
    [
        pytest.param(None, "capability_unavailable", id="absent"),
        pytest.param(
            {"USERS_GRANT_CAPABILITY": "not-ciphertext"}, "capability_unreadable", id="bad"
        ),
    ],
)
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_an_unusable_stored_capability_fails_closed_and_is_never_replaced(
    operation, secrets, reason
):
    env = operation_env(operation, secrets=secrets, stored=secrets is not None)

    await process_operation(env)

    assert env.product.requests == []
    assert_retryable_failure(env, reason)
    assert_deployed_nothing(env)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "refuse", "access_payload", "reason"),
    [
        pytest.param("grant", "/users/grant", None, "grant_rejected", id="grant-refused"),
        pytest.param("revoke", "/users/revoke", None, "revoke_rejected", id="revoke-refused"),
        pytest.param("grant", "/users/access", None, "access_rejected", id="readback-refused"),
        pytest.param(
            "grant",
            None,
            {**QA_IDENTITY, "status": "unknown"},
            "malformed_access",
            id="readback-malformed",
        ),
        pytest.param(
            "revoke",
            None,
            {**QA_IDENTITY, "status": "active"},
            "active",
            id="revoke-still-active",
        ),
        pytest.param(
            "grant",
            None,
            {**QA_IDENTITY, "status": "inactive"},
            "inactive",
            id="grant-not-active",
        ),
    ],
)
async def test_an_unproved_readback_fails_retryably_without_the_capability(
    operation, refuse, access_payload, reason
):
    env = operation_env(operation)
    env.product.refuse = refuse
    env.product.access_payload = access_payload

    await process_operation(env)

    assert_retryable_failure(env, reason)
    assert_credential_safe(env)
    assert_deployed_nothing(env)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        pytest.param({"metadata": {USERS_GRANT_INTENT_KEY: "intent-1"}}, id="grant-intent"),
        pytest.param(
            {
                "message": {
                    "action": DeployAction.STOP.value,
                    "application_id": TARGET_APPLICATION_ID,
                }
            },
            id="lifecycle-action",
        ),
    ],
)
async def test_conflicting_access_metadata_fails_closed_without_deploying(conflict):
    env = operation_env("grant")
    env.api.run_metadata.update(conflict.get("metadata", {}))
    env.api.get_users_grant_intent.return_value = SimpleNamespace(
        project_id=PROJECT_ID, target_sha=HEAD_SHA, execution_run_id=TASK_ID
    )

    await process_operation(env, **conflict.get("message", {}))

    assert env.product.requests == []
    assert env.api.allocation_reads == []
    assert_retryable_failure(env, "does not match")
    assert_deployed_nothing(env)


def _lose_lock(env) -> None:
    env.stream.redis.value = "another-deploy"


def _assert_lock_lost(env, result: dict) -> None:
    assert result["reason"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    [write] = env.api.run_writes
    assert write["result"]["deploy_outcome"] == DeployOutcome.DEPLOY_LOCK_LOST.value


def _assert_withdrawn(env, result: dict) -> None:
    assert result["status"] == "cancelled"
    [write] = env.api.run_writes
    assert write["status"] == RunStatus.CANCELLED.value
    assert write["result"]["deploy_outcome"] == DeployOutcome.CANCELLED.value


@pytest.mark.asyncio
async def test_a_flagged_cleanup_stops_each_earlier_workflow_before_revoking():
    env = operation_env("revoke")
    env.github.runs = {41: "in_progress", 42: "queued"}

    result = await process_operation(env, fence_active_deploys=True)

    assert result["status"] == "success"
    assert sorted(env.github.cancels()) == [41, 42]
    events = env.api.events
    cancels = [index for index, event in enumerate(events) if event.startswith("cancel:")]
    # Every cancellation is the first write after a renewed admission.
    assert all(events[index - 1] == "claim" for index in cancels)
    assert events[-3:] == ["claim", "/users/revoke", "/users/access"]
    assert max(cancels) < events.index("/users/revoke")
    assert proves_operation(env.api.run_writes[0])
    assert_deployed_nothing(env)


@pytest.mark.asyncio
async def test_a_lock_lost_after_listing_cancels_nothing():
    env = operation_env("revoke")
    env.github.runs = {41: "in_progress", 42: "queued"}
    env.github.on_list = lambda: _lose_lock(env)

    result = await process_operation(env, fence_active_deploys=True)

    assert env.github.writes() == []
    assert env.product.requests == []
    _assert_lock_lost(env, result)


@pytest.mark.asyncio
async def test_a_lock_lost_between_cancellations_stops_the_next_one():
    env = operation_env("revoke")
    env.github.runs = {41: "in_progress", 42: "queued"}
    env.github.on_cancel = lambda run_id: _lose_lock(env)

    result = await process_operation(env, fence_active_deploys=True)

    assert len(env.github.cancels()) == 1
    assert env.product.requests == []
    _assert_lock_lost(env, result)


@pytest.mark.asyncio
async def test_a_withdrawal_between_cancellations_stops_the_next_one_and_the_revoke():
    env = operation_env("revoke")
    env.github.runs = {41: "in_progress", 42: "queued"}
    env.github.on_cancel = lambda run_id: env.api.withdraw()

    result = await process_operation(env, fence_active_deploys=True)

    assert len(env.github.cancels()) == 1
    assert env.product.requests == []
    _assert_withdrawn(env, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("deployed", [True, False], ids=["deployed", "undeployed"])
@pytest.mark.parametrize("source", ["message-flag", "grant-run-evidence"])
async def test_an_unproven_workflow_stop_fails_the_cleanup_before_any_effect(deployed, source):
    env = operation_env("revoke")
    if not deployed:
        env.api.allocations[TARGET_APPLICATION_ID] = []
    env.api.history = [dispatched_grant_run(env)]
    env.github.runs = {7: "in_progress"}
    env.github.refuse_cancel = True

    await process_operation(env, fence_active_deploys=source == "message-flag")

    assert env.product.requests == []
    assert_retryable_failure(env, "could not be proven stopped")
    assert_deployed_nothing(env)


@pytest.mark.asyncio
async def test_an_operation_owing_no_stop_starts_no_github_work():
    env = operation_env("revoke")
    env.github.runs = {7: "in_progress"}

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.github.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "deployed"),
    [("grant", True), ("revoke", True), ("revoke", False)],
    ids=["grant", "revoke", "revoke-undeployed"],
)
async def test_a_withdrawal_during_the_target_read_prevents_every_effect(operation, deployed):
    env = operation_env(operation)
    if not deployed:
        env.api.allocations[TARGET_APPLICATION_ID] = []
    env.api.on_allocation_read = env.api.withdraw

    result = await process_operation(env, fence_active_deploys=True)

    assert "claim-refused" in env.api.events
    assert env.product.requests == []
    assert env.github.writes() == []
    _assert_withdrawn(env, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("deployed", [True, False], ids=["deployed", "undeployed"])
async def test_an_unreadable_run_history_never_proves_cleanup(deployed):
    env = operation_env("revoke")
    if not deployed:
        env.api.allocations[TARGET_APPLICATION_ID] = []
    env.api.history_unreadable = True

    result = await process_operation(env)

    assert result["status"] == "failed"
    assert env.product.requests == []
    assert env.github.requests == []
    [write] = env.api.run_writes
    assert not proves_operation(write)


@pytest.mark.asyncio
async def test_a_history_without_the_current_grant_run_owes_the_stop():
    env = operation_env("revoke")
    env.api.history = []
    env.github.runs = {7: "in_progress"}

    result = await process_operation(env)

    assert result["status"] == "success"
    assert env.github.cancels() == [7]
    assert env.api.events.index("cancel:7") < env.api.events.index("/users/revoke")
