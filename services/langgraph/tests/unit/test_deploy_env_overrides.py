"""Deploy-time environment overrides and the redundant-deploy shortcut.

The shortcut exists so a repeated deploy of the same commit does not redo work.
A deploy that changes the environment is not that case: swallowing it would drop
the change, including a redeploy whose whole point is to remove a value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.env_overrides import EMPTY_OVERRIDES_DIGEST, env_overrides_digest
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from src.consumers.deploy import _already_deployed_application, _effective_env_overrides
from tests.unit.factories import make_project, make_repository

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


def _api(deployments: list[dict]) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=deployments)
    client.get_application = AsyncMock(return_value=_Running())
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
