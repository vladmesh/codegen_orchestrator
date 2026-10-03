"""The project deploy lock is a fence that every deploy write checks.

Redis here is fakeredis with Lua (`fakeredis[lua]`): the claim is a real
`SET NX EX`, the hold check and the release are the real scripts, and an expired
lock really expires. Everything else a deploy talks to — the API, GitHub, the
registry, the deployed product, SSH — is a double, and the deploy path between
them is the real one: the consumer, the DevOps subgraph and the result handlers.

Losing the lock is staged the way it happens in production: deploy A's lock
expires under it, deploy B claims it with `SET NX`, and A carries on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import httpx
import pytest

from shared.contracts.dto.deploy_dispatch import DeployDispatchClaim
from shared.contracts.dto.product_brief import InitialSetting, ProductBriefContent
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.users_grant import USERS_GRANT_INTENT_KEY, GrantIntent, GrantIntentKind
from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger
from src.clients.product_settings import SettingSeedProof
from src.clients.users_grant import GrantProof
from src.deploy_fence import DeployFence, DeployFenceLost, DeployWrite
from tests.unit.factories import (
    held_deploy_fence,
    make_product_brief,
    make_project,
    make_repository,
    make_run,
    make_run_start,
)

PROJECT_ID = "proj-fence"
TASK_ID = "deploy-a"
LOCK_KEY = f"deploy:{PROJECT_ID}:lock"
OTHER_DEPLOY = "deploy-b:4f1c"
HEAD_SHA = "a" * 40
BUILT_SHA = "b" * 40
SERVER_IP = "192.0.2.10"
INTENT = GrantIntent(
    id="users-grant-initial-owner-deploy-a",
    kind=GrantIntentKind.INITIAL_OWNER,
    project_id=PROJECT_ID,
    channel="telegram",
    external_id="12345",
    target_sha=HEAD_SHA,
    initiating_actor="deploy_producer",
    execution_run_id=TASK_ID,
)
_DERIVED = {"source": "derived", "required": True, "environments": ["production"]}
_GENERATED = {"source": "generated_secret", "required": True, "environments": ["production"]}
CONTRACT = {
    "version": "1",
    "entries": {
        "PUBLIC_BASE_URL": _DERIVED,
        "BACKEND_IMAGE": _DERIVED,
        "USERS_GRANT_CAPABILITY": _GENERATED,
        "SETTINGS_WRITE_CAPABILITY": _GENERATED,
    },
}


@dataclass
class Deploy:
    """One deploy of `PROJECT_ID`, wired to doubles of everything but Redis."""

    redis: fakeredis.aioredis.FakeRedis
    stream: AsyncMock
    api: MagicMock
    resolver_api: MagicMock
    deployer_api: MagicMock
    lifecycle_api: MagicMock
    github: AsyncMock
    grants: AsyncMock
    settings: AsyncMock
    ssh: AsyncMock

    async def run(self, **message) -> dict:
        from src.consumers.deploy import process_deploy_job

        job = {
            "task_id": TASK_ID,
            "project_id": PROJECT_ID,
            "telegram_chat_id": "12345",
            "callback_stream": "",
            "story_id": "story-1",
            "triggered_by": DeployTrigger.WEBHOOK.value,
            "action": "feature",
            "head_sha": HEAD_SHA,
            "deployed_commit_sha": BUILT_SHA,
            "fence_active_deploys": True,
            **message,
        }
        return await process_deploy_job(job, self.stream)

    def run_writes(self) -> list[dict]:
        """What this deploy wrote to its own Run, in order."""
        return [
            call.kwargs["json"]
            for call in self.api.patch.await_args_list
            if call.args == (f"runs/{TASK_ID}",)
        ]

    def run_outcomes(self) -> list[str]:
        return [
            write["result"]["deploy_outcome"] for write in self.run_writes() if "result" in write
        ]


@pytest.fixture
def deploy():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stream = AsyncMock()
    stream.redis = redis

    api = MagicMock()
    api.patch = AsyncMock()
    api.get_run = AsyncMock(
        return_value=make_run(id=TASK_ID, run_metadata={USERS_GRANT_INTENT_KEY: INTENT.id})
    )
    api.start_run = AsyncMock(return_value=make_run_start(run_id=TASK_ID))
    api.get_project = AsyncMock(return_value=make_project(config={"modules": ["backend"]}))
    api.get_primary_repository = AsyncMock(
        return_value=make_repository(git_url="https://github.com/org/fenced")
    )
    api.get_users_grant_intent = AsyncMock(return_value=INTENT)
    api.complete_users_grant_intent = AsyncMock()
    api.get_application = AsyncMock(return_value=MagicMock(id=42, server_handle="srv-1"))
    api.get_product_brief_by_story = AsyncMock(
        return_value=make_product_brief(
            story_id="story-1",
            content=ProductBriefContent(
                summary="A reminder bot",
                must_requirements=[{"id": "req-1", "text": "It must remind at a chosen hour"}],
                initial_settings=[InitialSetting(key="reminders.default_hour", value=9)],
            ),
        )
    )

    resolver_api = MagicMock()
    resolver_api.merge_secrets = AsyncMock()

    deployer_api = MagicMock()
    deployer_api.get = AsyncMock(return_value={"status": RunStatus.RUNNING.value})
    deployer_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
    deployer_api.get_server_ssh_key = AsyncMock(return_value="ssh-key")
    deployer_api.claim_deploy_dispatch = AsyncMock(
        side_effect=lambda run_id: DeployDispatchClaim(
            run_id=run_id,
            granted=True,
            run_status=RunStatus.RUNNING,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    deployer_api.get_primary_repository = AsyncMock(return_value=make_repository())
    deployer_api.get_or_create_application = AsyncMock(return_value={"id": 42})
    deployer_api.update_application = AsyncMock()
    deployer_api.create_deployment = AsyncMock()

    lifecycle_api = MagicMock()
    lifecycle_api.get_server = AsyncMock(
        return_value=MagicMock(public_ip=SERVER_IP, ssh_user="dev")
    )
    lifecycle_api.get_server_ssh_key = AsyncMock(return_value="ssh-key")

    github = AsyncMock()
    github.set_repository_secrets = AsyncMock(side_effect=lambda owner, repo, values: len(values))
    github.fence_workflow = AsyncMock(return_value=[])
    github.wait_for_workflow_completion = AsyncMock(
        return_value={
            "id": 1,
            "status": "completed",
            "conclusion": "success",
            "head_sha": BUILT_SHA,
        }
    )

    grants = AsyncMock()
    grants.grant_and_resolve = AsyncMock(return_value=GrantProof(active=True))
    settings = AsyncMock()
    settings.seed_and_resolve = AsyncMock(
        side_effect=lambda values, capability: [SettingSeedProof(written=True) for _ in values]
    )

    ssh = AsyncMock()
    ssh.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="", stderr=""))
    ssh_module = MagicMock()
    ssh_module.import_private_key = MagicMock(return_value="key")
    ssh_module.connect = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=ssh), __aexit__=AsyncMock(return_value=None)
        )
    )

    real_client = httpx.AsyncClient
    healthy = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "ok"}))
    allocations = {
        "backend": {
            "server_handle": "srv-1",
            "server_ip": SERVER_IP,
            "port": 8080,
            "service_name": "backend",
            "application_id": 42,
        }
    }
    with ExitStack() as stack:
        for module in (
            "deploy",
            "deploy_result_handler",
            "deploy_failure_handler",
            "deploy_precheck",
        ):
            stack.enter_context(patch(f"src.consumers.{module}.api_client", api))
        stack.enter_context(patch("src.consumers.deploy_lifecycle.api_client", lifecycle_api))
        stack.enter_context(patch("src.consumers.deploy_lifecycle.asyncssh", ssh_module))
        stack.enter_context(
            patch("src.consumers.deploy._run_deploy_precheck", AsyncMock(return_value=None))
        )
        stack.enter_context(
            patch("src.allocations.ensure_project_allocations", AsyncMock(return_value=allocations))
        )
        stack.enter_context(
            patch(
                "src.subgraphs.devops.env_contract_loader._fetch_env_contract",
                AsyncMock(return_value=CONTRACT),
            )
        )
        stack.enter_context(patch("src.subgraphs.devops.secret_resolver.api_client", resolver_api))
        stack.enter_context(patch("src.subgraphs.devops.deployer.api_client", deployer_api))
        stack.enter_context(
            patch("src.subgraphs.devops.deployer.GitHubAppClient", return_value=github)
        )
        stack.enter_context(
            patch(
                "src.subgraphs.devops.deployer.verify_published_images",
                AsyncMock(return_value={"BACKEND_IMAGE": "sha256:" + "d" * 64}),
            )
        )
        stack.enter_context(
            patch(
                "src.subgraphs.devops.smoke.httpx.AsyncClient",
                side_effect=lambda: real_client(transport=healthy),
            )
        )
        stack.enter_context(
            patch("src.consumers.deploy_result_handler.GeneratedServiceGrantClient")
        ).return_value = grants
        stack.enter_context(
            patch("src.consumers.deploy_result_handler.GeneratedServiceSettingsClient")
        ).return_value = settings
        yield Deploy(
            redis=redis,
            stream=stream,
            api=api,
            resolver_api=resolver_api,
            deployer_api=deployer_api,
            lifecycle_api=lifecycle_api,
            github=github,
            grants=grants,
            settings=settings,
            ssh=ssh,
        )


async def _expire_and_hand_over(fence: DeployFence) -> None:
    """Deploy A's lock runs out, and deploy B claims the project the ordinary way."""
    await fence.redis.pexpire(fence.lock_key, 1)
    await asyncio.sleep(0.01)
    assert await fence.redis.get(fence.lock_key) is None
    assert await DeployFence(fence.redis, fence.project_id, OTHER_DEPLOY).acquire(3600)


async def _replace(fence: DeployFence) -> None:
    """Deploy A's lock is overwritten while it is still running."""
    await fence.redis.set(fence.lock_key, OTHER_DEPLOY, ex=3600)


@dataclass
class LockLoss:
    """Takes deploy A's lock away the moment it checks before one chosen write."""

    write: DeployWrite
    occurrence: int = 1
    how: Callable = _expire_and_hand_over
    seen: int = 0

    @property
    def fired(self) -> bool:
        return self.seen >= self.occurrence

    def install(self, monkeypatch) -> None:
        check = DeployFence.ensure_held
        loss = self

        async def ensure_held(fence: DeployFence, write: DeployWrite) -> None:
            if write is loss.write:
                loss.seen += 1
                if loss.seen == loss.occurrence:
                    await loss.how(fence)
            await check(fence, write)

        monkeypatch.setattr(DeployFence, "ensure_held", ensure_held)


def _completed_run_written(d: Deploy) -> bool:
    return any(write.get("status") == RunStatus.COMPLETED.value for write in d.run_writes())


#: Every write of a commit deploy, in the order the deploy makes it, and how a
#: test sees whether it was performed. The pin tag is written twice: created
#: before the dispatch, and removed after the run.
DEPLOY_WRITES = [
    pytest.param(DeployWrite.RUN_STATE, 1, lambda d: d.api.start_run.await_count, id="run-start"),
    pytest.param(
        DeployWrite.SECRET_PERSISTENCE,
        1,
        lambda d: d.resolver_api.merge_secrets.await_count,
        id="secret-persistence",
    ),
    pytest.param(
        DeployWrite.GITHUB_SECRETS,
        1,
        lambda d: d.github.set_repository_secrets.await_count,
        id="github-secrets",
    ),
    pytest.param(
        DeployWrite.WORKFLOW_FENCE,
        1,
        lambda d: d.github.fence_workflow.await_count,
        id="stop-older-runs",
    ),
    pytest.param(
        DeployWrite.WORKFLOW_PIN_TAG,
        1,
        lambda d: d.github.create_or_reset_tag.await_count,
        id="pin-tag-create",
    ),
    pytest.param(
        DeployWrite.WORKFLOW_DISPATCH,
        1,
        lambda d: (
            d.github.trigger_workflow_dispatch.await_count
            + d.deployer_api.claim_deploy_dispatch.await_count
        ),
        id="workflow-dispatch",
    ),
    pytest.param(
        DeployWrite.WORKFLOW_PIN_TAG,
        2,
        lambda d: d.github.delete_ref.await_count,
        id="pin-tag-remove",
    ),
    pytest.param(
        DeployWrite.DEPLOYMENT_RECORD,
        1,
        lambda d: (
            d.deployer_api.update_application.await_count
            + d.deployer_api.create_deployment.await_count
        ),
        id="deployment-record",
    ),
    pytest.param(
        DeployWrite.PRODUCT_ACCESS,
        1,
        lambda d: (
            d.grants.grant_and_resolve.await_count + d.api.complete_users_grant_intent.await_count
        ),
        id="owner-grant",
    ),
    pytest.param(
        DeployWrite.PRODUCT_SETTINGS,
        1,
        lambda d: d.settings.seed_and_resolve.await_count,
        id="settings-seed",
    ),
    pytest.param(DeployWrite.RUN_STATE, 2, _completed_run_written, id="run-result"),
]


def _assert_stopped_cleanly(d: Deploy, result: dict) -> None:
    """A ends as `deploy_lock_lost`, recorded once and last.

    Last rather than only: a lifecycle action records its Run before it moves the
    application's status, so losing the lock between the two leaves that record
    behind `deploy_lock_lost`.
    """
    assert result["status"] == "failed"
    assert result["reason"] == DeployOutcome.DEPLOY_LOCK_LOST.value
    assert d.run_outcomes().count(DeployOutcome.DEPLOY_LOCK_LOST.value) == 1
    assert d.run_writes()[-1]["status"] == RunStatus.FAILED.value
    assert d.run_writes()[-1]["result"]["deploy_outcome"] == DeployOutcome.DEPLOY_LOCK_LOST.value


@pytest.mark.asyncio
async def test_a_deploy_that_keeps_its_lock_makes_every_write_and_releases_it(deploy):
    """The control for the table below: each write it watches is really made."""
    result = await deploy.run()

    assert result["status"] == "success"
    for case in DEPLOY_WRITES:
        _, _, performed = case.values
        assert performed(deploy), case.id
    assert deploy.run_outcomes()[-1] == DeployOutcome.SUCCESS.value
    # A normal successful deploy releases its own lock.
    assert await deploy.redis.get(LOCK_KEY) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("write", "occurrence", "performed"), DEPLOY_WRITES)
async def test_no_deploy_write_is_made_after_the_lock_is_lost(
    deploy, monkeypatch, write, occurrence, performed
):
    loss = LockLoss(write, occurrence)
    loss.install(monkeypatch)

    result = await deploy.run()

    assert loss.fired
    assert not performed(deploy)
    _assert_stopped_cleanly(deploy, result)
    assert await deploy.redis.get(LOCK_KEY) == OTHER_DEPLOY


@pytest.mark.asyncio
async def test_a_rerun_is_a_dispatch_and_is_refused_without_the_lock(deploy, monkeypatch):
    deploy.github.wait_for_workflow_completion.side_effect = RuntimeError("deploy job failed")
    deploy.github.get_latest_workflow_run.return_value = {"id": 7}
    loss = LockLoss(DeployWrite.WORKFLOW_DISPATCH, occurrence=2)
    loss.install(monkeypatch)

    result = await deploy.run()

    assert loss.fired
    deploy.github.trigger_workflow_dispatch.assert_awaited_once()
    deploy.github.rerun_failed_jobs.assert_not_awaited()
    _assert_stopped_cleanly(deploy, result)
    assert await deploy.redis.get(LOCK_KEY) == OTHER_DEPLOY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "occurrence", "performed"),
    [
        pytest.param(
            DeployWrite.REMOTE_EXECUTION, 1, lambda d: d.ssh.run.await_count, id="remote-stop"
        ),
        pytest.param(DeployWrite.RUN_STATE, 2, _completed_run_written, id="lifecycle-run-result"),
        pytest.param(
            DeployWrite.APPLICATION_STATE,
            1,
            lambda d: [c for c in d.api.patch.await_args_list if c.args == ("applications/42",)],
            id="application-status",
        ),
    ],
)
async def test_no_lifecycle_write_is_made_after_the_lock_is_lost(
    deploy, monkeypatch, write, occurrence, performed
):
    deploy.api.get_run.return_value = make_run(id=TASK_ID)
    loss = LockLoss(write, occurrence)
    loss.install(monkeypatch)

    result = await deploy.run(action="stop", application_id=42, head_sha="", deployed_commit_sha="")

    assert loss.fired
    assert not performed(deploy)
    _assert_stopped_cleanly(deploy, result)
    assert await deploy.redis.get(LOCK_KEY) == OTHER_DEPLOY


@pytest.mark.asyncio
@pytest.mark.parametrize("how", [_expire_and_hand_over, _replace], ids=["expired", "replaced"])
async def test_a_stale_holder_is_refused_and_leaves_the_new_holder_alone(deploy, monkeypatch, how):
    """Deploy A holds the lock; it expires or is replaced and deploy B holds it.

    A's next write — here the repository secrets its workflow would deploy — is
    refused and not made, A ends `deploy_lock_lost`, and B's lock is untouched.
    """
    loss = LockLoss(DeployWrite.GITHUB_SECRETS, how=how)
    loss.install(monkeypatch)

    result = await deploy.run()

    assert loss.fired
    deploy.github.set_repository_secrets.assert_not_awaited()
    deploy.github.trigger_workflow_dispatch.assert_not_awaited()
    _assert_stopped_cleanly(deploy, result)
    assert await deploy.redis.get(LOCK_KEY) == OTHER_DEPLOY
    assert await deploy.redis.ttl(LOCK_KEY) > 0


@pytest.mark.asyncio
async def test_a_deploy_that_did_not_get_the_lock_does_not_delete_it(deploy):
    await deploy.redis.set(LOCK_KEY, OTHER_DEPLOY, ex=3600)

    result = await deploy.run()

    assert result["status"] == "cancelled"
    assert deploy.run_outcomes() == [DeployOutcome.CANCELLED.value]
    deploy.api.start_run.assert_not_awaited()
    assert await deploy.redis.get(LOCK_KEY) == OTHER_DEPLOY


@pytest.mark.asyncio
async def test_release_leaves_a_lock_whose_token_changed():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    fence = DeployFence.for_job(redis, PROJECT_ID, TASK_ID)
    assert await fence.acquire(3600)
    await redis.set(fence.lock_key, OTHER_DEPLOY)

    await fence.release()

    assert await redis.get(fence.lock_key) == OTHER_DEPLOY


@pytest.mark.asyncio
async def test_two_claims_of_one_job_never_share_a_token():
    """A redelivered message is a second holder, not the first one back."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = DeployFence.for_job(redis, PROJECT_ID, TASK_ID)
    assert await first.acquire(3600)
    await redis.delete(first.lock_key)
    second = DeployFence.for_job(redis, PROJECT_ID, TASK_ID)
    assert await second.acquire(3600)

    assert first.token != second.token
    with pytest.raises(DeployFenceLost):
        await first.ensure_held(DeployWrite.RUN_STATE)
    await first.release()
    assert await redis.get(second.lock_key) == second.token


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_no_temporary_access_operation_is_made_after_the_lock_is_lost(monkeypatch, operation):
    """A QA capability redeploy writes the identity's access only under its lock."""
    from src.consumers.deploy_result_handler import _handle_deploy_success
    from tests.unit.consumers.test_deploy_routing import _make_deploy_msg, _temporary_grant

    grant = _temporary_grant()
    fence = held_deploy_fence(project_id="proj-1", task_id=grant.grant_run_id)
    loss = LockLoss(DeployWrite.PRODUCT_ACCESS)
    loss.install(monkeypatch)

    with (
        patch("src.consumers.deploy_result_handler.api_client") as api,
        patch("src.consumers.deploy_result_handler.GeneratedServiceGrantClient") as client,
        pytest.raises(DeployFenceLost),
    ):
        api.get_temporary_access_grant = AsyncMock(return_value=grant)
        api.patch = AsyncMock()
        await _handle_deploy_success(
            result={
                "deployed_url": grant.target_base_url,
                "secret_values": {"USERS_GRANT_CAPABILITY": "capability"},
            },
            smoke_result=None,
            task_id=grant.grant_run_id,
            project_id="proj-1",
            project=make_project(),
            callback_stream="",
            telegram_chat_id="",
            story_id="",
            redis=AsyncMock(),
            msg=_make_deploy_msg(task_id=grant.grant_run_id),
            fence=fence,
            application_id=grant.target_application_id,
            temporary_access_grant=grant,
            temporary_access_operation=operation,
        )

    assert loss.fired
    client.assert_not_called()
    api.patch.assert_not_awaited()
    assert await fence.redis.get(fence.lock_key) == OTHER_DEPLOY
