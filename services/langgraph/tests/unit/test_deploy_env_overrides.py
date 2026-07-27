"""Deploy-time environment overrides and the redundant-deploy shortcut.

The shortcut exists so a repeated deploy of the same commit does not redo work.
A deploy that changes the environment is not that case: swallowing it would drop
the change, including a redeploy whose whole point is to remove a value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.env_overrides import EMPTY_OVERRIDES_DIGEST, env_overrides_digest
from shared.contracts.queues.deploy import DeployAction, DeployMessage
from src.consumers.deploy import _already_deployed_application

HEAD = "a" * 40
ALLOCATED = {"backend": {"application_id": 7}}


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
