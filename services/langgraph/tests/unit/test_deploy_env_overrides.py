"""Deploy-time environment overrides and the redundant-deploy shortcut.

The shortcut exists so a repeated deploy of the same commit does not redo work.
A deploy that changes the environment is not that case: swallowing it would drop
the change, including a redeploy whose whole point is to remove a value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.env_overrides import EMPTY_OVERRIDES_DIGEST, env_overrides_digest
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployOutcome,
    DeployTrigger,
)
from src.consumers.deploy import _already_deployed_application, _effective_env_overrides
from tests.unit.factories import make_project, make_repository, make_run_start

HEAD = "a" * 40
ALLOCATED = {"backend": {"application_id": 7}}
ALLOCATED_RUNNING = {"backend": {"server_ip": "1.2.3.4", "port": 8080, "application_id": 7}}


def _deployment(sha: str, digest: str | None) -> dict:
    info: dict = {"branch": "main"}
    if digest is not None:
        info["env_overrides_digest"] = digest
    return {"deployed_sha": sha, "deployment_info": info}


class _Running:
    status = "running"


def _api(
    deployments: list[dict],
    run_status: RunStatus = RunStatus.QUEUED,
    start_status: RunStatus | None = None,
) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=deployments)
    client.get_application = AsyncMock(return_value=_Running())
    # The consumer reads its own run before acting on the message, then takes it
    # to running as a locked transition that a cancellation refuses.
    client.get_run = AsyncMock(return_value=SimpleNamespace(status=run_status))
    started = start_status or RunStatus.RUNNING
    client.start_run = AsyncMock(
        return_value=make_run_start(started=started is RunStatus.RUNNING, run_status=started)
    )
    return client


def test_digest_ignores_key_order() -> None:
    assert env_overrides_digest({"A": "1", "B": "2"}) == env_overrides_digest({"B": "2", "A": "1"})


def test_absent_and_empty_overrides_are_the_same() -> None:
    assert env_overrides_digest(None) == EMPTY_OVERRIDES_DIGEST
    assert env_overrides_digest({}) == EMPTY_OVERRIDES_DIGEST


def test_different_values_differ() -> None:
    assert env_overrides_digest({"A": "1"}) != env_overrides_digest({"A": "2"})


def test_digest_does_not_leak_values() -> None:
    assert "secret-value" not in env_overrides_digest({"A": "secret-value"})


@pytest.mark.asyncio
async def test_same_commit_and_no_overrides_is_redundant() -> None:
    with patch("src.consumers.deploy.api_client", _api([_deployment(HEAD, None)])):
        assert await _already_deployed_application(ALLOCATED, HEAD, {}) == 7


@pytest.mark.asyncio
async def test_same_commit_with_new_override_is_not_redundant() -> None:
    """Turning a value on must reach the server."""

    with patch("src.consumers.deploy.api_client", _api([_deployment(HEAD, None)])):
        assert await _already_deployed_application(ALLOCATED, HEAD, {"TG_BOT_TEST": "5"}) is None


@pytest.mark.asyncio
async def test_removing_an_override_is_not_redundant() -> None:
    """Revocation is a deploy of the same commit with the value gone."""

    recorded = _deployment(HEAD, env_overrides_digest({"TG_BOT_TEST": "5"}))
    with patch("src.consumers.deploy.api_client", _api([recorded])):
        assert await _already_deployed_application(ALLOCATED, HEAD, {}) is None


@pytest.mark.asyncio
async def test_same_overrides_stay_redundant() -> None:
    overrides = {"TG_BOT_TEST": "5"}
    recorded = _deployment(HEAD, env_overrides_digest(overrides))
    with patch("src.consumers.deploy.api_client", _api([recorded])):
        assert await _already_deployed_application(ALLOCATED, HEAD, overrides) == 7


@pytest.mark.asyncio
async def test_records_without_a_digest_count_as_no_overrides() -> None:
    """Deployments written before the field existed set nothing extra."""

    with patch("src.consumers.deploy.api_client", _api([_deployment(HEAD, None)])):
        assert await _already_deployed_application(ALLOCATED, HEAD, None) == 7


def test_deploy_message_carries_overrides() -> None:
    msg = DeployMessage(
        task_id="t",
        project_id="p",
        action=DeployAction.CREATE,
        head_sha=HEAD,
        env_overrides={"TG_BOT_TEST_TELEGRAM_ID": "5"},
    )

    assert msg.env_overrides == {"TG_BOT_TEST_TELEGRAM_ID": "5"}


def test_deploy_message_defaults_to_no_overrides() -> None:
    msg = DeployMessage(task_id="t", project_id="p", action=DeployAction.CREATE, head_sha=HEAD)

    assert msg.env_overrides == {}


@pytest.mark.parametrize(("configured_audience", "message_audience"), [("", "42"), ("42", "84")])
def test_deploy_cannot_override_the_configured_bot_audience(
    configured_audience: str, message_audience: str
) -> None:
    project = SimpleNamespace(
        config={
            "bot_access": {
                "mode": "public" if not configured_audience else "only_me",
                "allowed_telegram_ids": configured_audience,
            },
            "env_overrides": {"TG_BOT_ALLOWED_TELEGRAM_IDS": configured_audience},
        }
    )

    with pytest.raises(ValueError, match="cannot override"):
        _effective_env_overrides(project, {"TG_BOT_ALLOWED_TELEGRAM_IDS": message_audience})


def test_legacy_private_bot_rejects_a_deploy_audience_override() -> None:
    project = SimpleNamespace(
        config={
            "secrets": {"ADMIN_TELEGRAM_ID": "encrypted"},
        }
    )

    with pytest.raises(ValueError, match="legacy private bot"):
        _effective_env_overrides(project, {"TG_BOT_ALLOWED_TELEGRAM_IDS": ""})


@pytest.mark.asyncio
async def test_repeating_a_landed_revoke_is_redundant() -> None:
    """Revoking access that is already gone must not be an error.

    The reconciler retries until it sees a successful deploy, so a retry that
    arrives after the value was cleared has to be cheap and successful rather
    than a second rollout.
    """

    cleared = {"TG_BOT_TEST_TELEGRAM_ID": ""}
    recorded = _deployment(HEAD, env_overrides_digest(cleared))
    with patch("src.consumers.deploy.api_client", _api([recorded])):
        assert await _already_deployed_application(ALLOCATED, HEAD, cleared) == 7


def _contract_with(keys: list[str]) -> dict:
    return {
        "entries": {
            key: {
                "source": "literal",
                "value": "",
                "environments": ["production"],
                "consumers": ["tg_bot"],
                "required": False,
            }
            for key in keys
        }
    }


def test_deploy_reports_the_test_identity_slot_it_resolved() -> None:
    """The scheduler cannot read the generated repository; the deploy can.

    Granting a value the commit never declared would fail the next deploy, so
    the run that resolved the contract says whether the slot is there.
    """
    from src.consumers.deploy_result_handler import _declares_test_identity_slot

    assert _declares_test_identity_slot(
        {"environment_contract": _contract_with(["TG_BOT_TEST_TELEGRAM_ID"])}
    )
    assert not _declares_test_identity_slot(
        {"environment_contract": _contract_with(["TG_BOT_ALLOWED_TELEGRAM_IDS"])}
    )


def test_a_deploy_that_read_no_contract_reports_no_slot() -> None:
    from src.consumers.deploy_result_handler import _declares_test_identity_slot

    assert not _declares_test_identity_slot({"environment_contract": None})


def _fenced_job(**overrides) -> dict:
    job = {
        "task_id": "deploy-revoke-1",
        "project_id": "proj-1",
        "user_id": "",
        "callback_stream": "",
        "triggered_by": DeployTrigger.ADMIN.value,
        "action": DeployAction.FEATURE.value,
        "head_sha": HEAD,
        "env_overrides": {"TG_BOT_TEST_TELEGRAM_ID": ""},
        "fence_active_deploys": True,
    }
    job.update(overrides)
    return job


@pytest.mark.asyncio
async def test_a_fenced_deploy_runs_even_when_it_looks_redundant() -> None:
    """The shortcut cannot answer for a deploy that has to stop other runs.

    A revoke whose value is already recorded still has to reach the fence: the
    deploy that set the value may be live on Actions right now, and reporting
    this one successful without running it would call the access removed while
    it can still be written back.
    """
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.exists = AsyncMock(return_value=False)

    cleared = {"TG_BOT_TEST_TELEGRAM_ID": ""}
    api = _api([_deployment(HEAD, env_overrides_digest(cleared))])
    api.patch = AsyncMock()
    api.get_project = AsyncMock(
        return_value=make_project(name="my-project", config={"modules": ["backend"]})
    )
    api.get_primary_repository = AsyncMock(
        return_value=make_repository(git_url="https://github.com/org/my-project")
    )

    graph = AsyncMock()
    graph.ainvoke = AsyncMock(
        return_value={"deployed_url": "http://1.2.3.4:8080", "deployment_result": {}}
    )

    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
        patch("src.consumers.deploy_precheck._pre_check_server", AsyncMock(return_value=None)),
        patch(
            "src.allocations.ensure_project_allocations",
            AsyncMock(return_value=ALLOCATED_RUNNING),
        ),
        patch("src.consumers.deploy.create_devops_subgraph", return_value=graph),
    ):
        await process_deploy_job(_fenced_job(), redis)

    graph.ainvoke.assert_awaited_once()
    assert graph.ainvoke.await_args.args[0]["fence_active_deploys"] is True


@pytest.mark.asyncio
async def test_a_withdrawn_deploy_is_refused_when_its_message_is_picked_up_later() -> None:
    """A grant deploy the sweep gave up on must not still apply its value.

    The message can sit in the queue past the decision to revoke, and no fence on
    GitHub Actions reaches a run that never started. Its run is the record the
    consumer reads first, so a cancelled one ends the job before anything is
    deployed or any lock is taken.
    """
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.exists = AsyncMock(return_value=False)

    api = _api([], run_status=RunStatus.CANCELLED)
    api.patch = AsyncMock()

    graph = AsyncMock()

    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy.create_devops_subgraph", return_value=graph),
    ):
        result = await process_deploy_job(
            _fenced_job(task_id="deploy-grant-1", env_overrides={"TG_BOT_TEST_TELEGRAM_ID": "42"}),
            redis,
        )

    assert result["reason"] == "run_cancelled"
    graph.ainvoke.assert_not_called()
    api.patch.assert_not_awaited()
    redis.redis.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_withdrawal_landing_after_the_first_read_still_stops_the_deploy() -> None:
    """The read is an early-out, not the guard. The transition to running is.

    This is the narrow interleaving the earlier check cannot cover: the consumer
    reads a live run, and only then does the sweep withdraw it. A blind patch to
    running would put the cancelled run back into a state the dispatch claim
    accepts, so the revoke would clear the value, record the grant revoked, and
    this deploy would write the identity back afterwards.
    """
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.exists = AsyncMock(return_value=False)

    # Live when read, cancelled by the time the run is taken to running.
    api = _api([], run_status=RunStatus.QUEUED, start_status=RunStatus.CANCELLED)
    api.patch = AsyncMock()

    graph = AsyncMock()

    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy.create_devops_subgraph", return_value=graph),
    ):
        result = await process_deploy_job(
            _fenced_job(task_id="deploy-grant-1", env_overrides={"TG_BOT_TEST_TELEGRAM_ID": "42"}),
            redis,
        )

    assert result["reason"] == "run_cancelled"
    graph.ainvoke.assert_not_called()
    # Nothing resurrected the run, and nothing was deployed under it.
    api.patch.assert_not_awaited()
    api.get_project.assert_not_awaited()
    # The lock this job did take is released.
    redis.redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unfenced_repeat_of_the_same_deploy_still_takes_the_shortcut() -> None:
    """Control for the test above: the shortcut itself is unchanged."""
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.exists = AsyncMock(return_value=False)

    cleared = {"TG_BOT_TEST_TELEGRAM_ID": ""}
    api = _api([_deployment(HEAD, env_overrides_digest(cleared))])
    api.patch = AsyncMock()
    api.get_project = AsyncMock(
        return_value=make_project(name="my-project", config={"modules": ["backend"]})
    )

    graph = AsyncMock()

    with (
        patch("src.consumers.deploy.api_client", api),
        patch(
            "src.allocations.ensure_project_allocations",
            AsyncMock(return_value=ALLOCATED_RUNNING),
        ),
        patch("src.consumers.deploy.create_devops_subgraph", return_value=graph),
    ):
        result = await process_deploy_job(_fenced_job(fence_active_deploys=False), redis)

    assert result["reason"] == "already_deployed_same_sha"
    graph.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_a_cancelled_deploy_records_a_terminal_outcome() -> None:
    """A stopped deploy has to say so on its run, or nothing ever routes it.

    A revoke fences every unfinished deploy.yml run of the repository, so an
    ordinary story deploy being cancelled is a normal path here, not a teardown
    corner. Left RUNNING with no result, its run is skipped by every supervisor
    for good and the story behind it waits on a deploy nobody is carrying.
    """
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.exists = AsyncMock(return_value=False)

    api = _api([])
    api.patch = AsyncMock()
    api.get_project = AsyncMock(return_value=make_project(config={}))
    api.get_primary_repository = AsyncMock(return_value=make_repository())

    graph = AsyncMock()
    graph.ainvoke = AsyncMock(return_value={"deployment_result": {"status": "cancelled"}})

    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
        patch("src.consumers.deploy_precheck._pre_check_server", AsyncMock(return_value=None)),
        patch(
            "src.allocations.ensure_project_allocations",
            AsyncMock(return_value=ALLOCATED_RUNNING),
        ),
        patch("src.consumers.deploy.create_devops_subgraph", return_value=graph),
    ):
        result = await process_deploy_job(_fenced_job(), redis)

    assert result["status"] == "cancelled"
    terminal = [
        call.kwargs["json"]
        for call in api.patch.await_args_list
        if call.kwargs["json"].get("status") == RunStatus.CANCELLED.value
    ]
    assert len(terminal) == 1
    assert terminal[0]["result"]["deploy_outcome"] == DeployOutcome.CANCELLED.value


@pytest.mark.asyncio
async def test_a_deploy_that_loses_the_lock_records_a_terminal_outcome() -> None:
    """Same reason: the story is still owed a deploy, so its run must be routable."""
    from src.consumers.deploy import process_deploy_job

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=None)  # somebody else holds the lock
    redis.redis.exists = AsyncMock(return_value=False)

    api = _api([])
    api.patch = AsyncMock()

    with patch("src.consumers.deploy.api_client", api):
        result = await process_deploy_job(_fenced_job(), redis)

    assert result["reason"] == "deploy_lock_held"
    patched = api.patch.await_args.kwargs["json"]
    assert patched["status"] == RunStatus.CANCELLED.value
    assert patched["result"]["deploy_outcome"] == DeployOutcome.CANCELLED.value
