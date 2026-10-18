"""Fake boundaries of a temporary-access operation driven through `process_deploy_job`.

The API, including the Run's dispatch claim and withdrawal, the allocation reads,
GitHub Actions and the deployed product are fakes at their boundaries. The product
speaks HTTP through `httpx.MockTransport` and GitHub through `respx`, so the real
capability client and the real workflow fence are what is exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import respx
from structlog.testing import capture_logs

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.deploy_dispatch import DISPATCH_CLAIMED_AT_KEY, DeployDispatchClaim
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantDTO,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.deploy import DeployAction, DeployOutcome, DeployTrigger
from shared.crypto import encrypt_dict
from src.clients.users_grant import GeneratedServiceGrantClient
from tests.unit.factories import (
    make_project,
    make_repository,
    make_run,
    make_run_start,
    make_server,
)

TASK_ID = "temporary-access-op-1"
PROJECT_ID = "proj-1"
TARGET_APPLICATION_ID = 17
OTHER_APPLICATION_ID = 11
TARGET_URL = "https://target.example.com"
HEAD_SHA = "a" * 40
CAPABILITY = "stored-capability-canary"
QA_IDENTITY = {"channel": "telegram", "external_id": "8202532144"}
NOW = datetime.now(UTC)
SERVER = make_server(handle="srv-1", public_ip="1.2.3.4", last_health_check=NOW)


def access_grant(operation: str, **overrides) -> TemporaryAccessGrantDTO:
    values = {
        "id": "tempaccess-qa-1",
        "project_id": PROJECT_ID,
        **QA_IDENTITY,
        "target_application_id": TARGET_APPLICATION_ID,
        "target_base_url": TARGET_URL,
        "head_sha": HEAD_SHA,
        "qa_run_id": "qa-1",
        "grant_run_id": TASK_ID if operation == "grant" else "temporary-access-grant-0",
        "revoke_run_id": TASK_ID if operation == "revoke" else None,
        "revoke_reason": (
            TemporaryAccessRevokeReason.RUN_TERMINAL if operation == "revoke" else None
        ),
        "qa_message": {
            "story_id": "story-1",
            "project_id": PROJECT_ID,
            "initiating_run_id": "deploy-1",
            "telegram_chat_id": "",
            "deployed_url": TARGET_URL,
            "application_id": TARGET_APPLICATION_ID,
            "acceptance_criteria": "bot admission",
            "run_id": "qa-1",
        },
        "status": (
            TemporaryAccessStatus.GRANTING
            if operation == "grant"
            else TemporaryAccessStatus.REVOKING
        ),
        "granted_at": NOW,
        "created_at": NOW,
    }
    values.update(overrides)
    return TemporaryAccessGrantDTO(**values)


def _application(application_id: int, *, server_handle: str = SERVER.handle) -> ApplicationDTO:
    return ApplicationDTO(
        id=application_id,
        repo_id="repo-1",
        server_handle=server_handle,
        service_name=f"app-{application_id}",
        status="running",
        reserved_ram_mb=512,
        created_at=NOW,
        updated_at=NOW,
    )


def _allocations(application_id: int) -> list[dict]:
    return [
        {
            "server_handle": SERVER.handle,
            "port": 8000 + index,
            "server_ip": SERVER.public_ip,
            "service_name": module,
            "application_id": application_id,
        }
        for index, module in enumerate(["backend", "postgres", "redis"])
    ]


def grant_run(
    grant: TemporaryAccessGrantDTO,
    run_id: str,
    status: RunStatus,
    *,
    claimed: bool = False,
    succeeded: bool = False,
):
    """A grant operation Run of `grant`, as the scheduler created it and its worker left it."""
    metadata = {"temporary_access_grant_id": grant.id, "temporary_access_operation": "grant"}
    if claimed:
        metadata[DISPATCH_CLAIMED_AT_KEY] = NOW.isoformat()
    return make_run(
        id=run_id,
        status=status,
        run_metadata=metadata,
        result=DeployRunResult(deploy_outcome=DeployOutcome.SUCCESS) if succeeded else None,
    )


def dispatched_grant_run(env, run_id: str | None = None):
    """A grant Run as a withdrawal leaves one that had crossed GitHub dispatch."""
    return grant_run(
        env.api.grant, run_id or env.api.grant.grant_run_id, RunStatus.CANCELLED, claimed=True
    )


class FakeApi:
    """The API slice an access operation reads and writes, with the Run kept like the API.

    It has no allocation-creating, deployment-record or secret-writing method, so
    a call to one fails the operation instead of passing silently.
    """

    def __init__(self, operation: str, *, secrets: dict | None) -> None:
        self.grant = access_grant(operation)
        self.current_grant = self.grant
        self.run_metadata = {
            "temporary_access_grant_id": self.grant.id,
            "temporary_access_operation": operation,
        }
        config = {"modules": ["backend"], "estimated_ram_mb": 512}
        if secrets is not None:
            config["secrets"] = secrets
        self.project = make_project(config=config)
        self.applications = {
            TARGET_APPLICATION_ID: _application(TARGET_APPLICATION_ID),
            OTHER_APPLICATION_ID: _application(OTHER_APPLICATION_ID),
        }
        self.allocations = {
            TARGET_APPLICATION_ID: _allocations(TARGET_APPLICATION_ID),
            OTHER_APPLICATION_ID: _allocations(OTHER_APPLICATION_ID),
        }
        self.grant_reads = 0
        self.run_status = RunStatus.QUEUED
        self.run_writes: list[dict] = []
        self.other_writes: list[tuple[str, dict]] = []
        self.allocation_reads: list[int] = []
        self.get_users_grant_intent = AsyncMock()
        self.events: list[str] = []
        self.on_allocation_read = None
        # The record's grant operation Runs: its current one finished, unclaimed,
        # so by default no earlier workflow is owed a stop.
        self.history = [
            grant_run(self.grant, self.grant.grant_run_id, RunStatus.COMPLETED, succeeded=True)
        ]
        self.history_unreadable = False

    async def get_run(self, run_id: str):
        assert run_id == TASK_ID
        return make_run(id=TASK_ID, status=self.run_status, run_metadata=self.run_metadata)

    async def list_project_runs(self, project_id: str, run_type: RunType):
        """`GET runs/?project_id&run_type`: the whole set, this operation Run included."""
        assert (project_id, run_type) == (PROJECT_ID, RunType.DEPLOY)
        if self.history_unreadable:
            request = httpx.Request("GET", "http://api/runs/")
            raise httpx.HTTPStatusError(
                "503", request=request, response=httpx.Response(503, request=request)
            )
        operation_run = make_run(id=TASK_ID, status=self.run_status, run_metadata=self.run_metadata)
        return [operation_run, *self.history]

    async def claim_deploy_dispatch(self, run_id: str) -> DeployDispatchClaim:
        """The locked dispatch claim: refused once the Run is terminal."""
        assert run_id == TASK_ID
        granted = self.run_status is RunStatus.RUNNING
        self.events.append("claim" if granted else "claim-refused")
        if granted:
            self.run_metadata[DISPATCH_CLAIMED_AT_KEY] = NOW.isoformat()
        return DeployDispatchClaim(
            run_id=run_id,
            granted=granted,
            run_status=self.run_status,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    def withdraw(self) -> None:
        """What `POST /runs/{id}/dispatch-withdraw` does to a Run not yet terminal."""
        self.run_status = RunStatus.CANCELLED

    async def start_run(self, task_id: str):
        self.run_status = RunStatus.RUNNING
        return make_run_start(run_id=task_id)

    async def get_project(self, project_id: str, **_kwargs):
        return self.project

    async def get_temporary_access_grant(self, grant_id: str):
        assert grant_id == self.grant.id
        self.grant_reads += 1
        return self.grant if self.grant_reads == 1 else self.current_grant

    async def get_application(self, application_id: int):
        return self.applications[application_id]

    async def get_application_allocations(self, application_id: int):
        self.allocation_reads.append(application_id)
        if self.on_allocation_read is not None:
            self.on_allocation_read()
        return self.allocations[application_id]

    async def get_server(self, handle: str):
        return SERVER

    async def list_active_incidents(self):
        return []

    async def get_primary_repository(self, project_id: str):
        return make_repository(git_url="https://github.com/org/product")

    async def patch(self, path: str, json: dict):
        if path != f"runs/{TASK_ID}":
            self.other_writes.append((path, json))
            return {}
        self.run_writes.append(json)
        self.run_status = RunStatus(json["status"])
        return {}


class FakeProduct:
    """The deployed product's users-core grant, revoke and access endpoints."""

    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.requests: list[httpx.Request] = []
        self.base_urls: list[str] = []
        self.refuse: str | None = None
        self.access_payload: object | None = None
        self.events: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        self.events.append(path)
        if path == self.refuse:
            return httpx.Response(500)
        if path == "/users/grant":
            self.active = True
            return httpx.Response(200, json={})
        if path == "/users/revoke":
            self.active = False
            return httpx.Response(200, json={})
        payload = self.access_payload or {
            **QA_IDENTITY,
            "status": "active" if self.active else "inactive",
        }
        return httpx.Response(200, json=payload)

    def client(self, base_url: str) -> GeneratedServiceGrantClient:
        self.base_urls.append(base_url)
        transport = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return GeneratedServiceGrantClient(base_url, transport=transport)

    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]


class FakeGitHub:
    """GitHub Actions of the product repository: run listing, cancellation, run status.

    A cancelled run reports completed on its next read, so the fence never waits.
    """

    def __init__(self, events: list[str]) -> None:
        self.runs: dict[int, str] = {}
        self.events = events
        self.requests: list[tuple[str, str]] = []
        self.refuse_cancel = False
        self.on_list = None
        self.on_cancel = None

    def client(self) -> GitHubAppClient:
        client = GitHubAppClient()
        client.get_token = AsyncMock(return_value="github-token")
        return client

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path.endswith("/actions/workflows/deploy.yml/runs"):
            status = request.url.params["status"]
            if self.on_list is not None:
                self.on_list()
            listed = [{"id": i, "status": s} for i, s in self.runs.items() if s == status]
            return httpx.Response(200, json={"workflow_runs": listed})
        if path.endswith("/cancel"):
            run_id = int(path.split("/")[-2])
            self.events.append(f"cancel:{run_id}")
            if self.refuse_cancel:
                return httpx.Response(422)
            self.runs[run_id] = "completed"
            if self.on_cancel is not None:
                self.on_cancel(run_id)
            return httpx.Response(202)
        run_id = int(path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"id": run_id, "status": self.runs[run_id]})

    def cancels(self) -> list[int]:
        return [int(path.split("/")[-2]) for _, path in self.requests if path.endswith("/cancel")]

    def writes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path in self.requests if method != "GET"]


class LockRedis:
    """The project deploy lock: `SET NX`, the hold check and compare-and-delete."""

    def __init__(self) -> None:
        self.value: str | None = None

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if self.value is not None:
            return False
        self.value = value
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        held = self.value == token
        if held and "DEL" in script:
            self.value = None
        return int(held)

    async def exists(self, key: str) -> int:
        return 0


def operation_env(operation: str, *, secrets: dict | None = None, stored: bool = True):
    """Everything an access operation could touch, wired to fakes."""
    if stored and secrets is None:
        secrets = encrypt_dict({"USERS_GRANT_CAPABILITY": CAPABILITY})
    api = FakeApi(operation, secrets=secrets)
    product = FakeProduct(active=operation == "revoke")
    product.events = api.events
    return SimpleNamespace(
        operation=operation,
        api=api,
        product=product,
        github=FakeGitHub(api.events),
        devops=MagicMock(),
        precheck=AsyncMock(return_value=None),
        stream=AsyncMock(redis=LockRedis()),
    )


async def process_operation(env, **message) -> dict:
    """Run `process_deploy_job` on the scheduler's operation message against `env`."""
    from src.consumers.deploy import process_deploy_job

    settings = SimpleNamespace(
        allocation_ram_reserve_mb=256, allocation_metrics_freshness_seconds=300
    )
    job = {
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "unaddressed_reason": "temporary QA capability operation",
        "callback_stream": "",
        "story_id": "",
        "triggered_by": DeployTrigger.ADMIN.value,
        "action": DeployAction.FEATURE.value,
        "head_sha": HEAD_SHA,
        "deployed_commit_sha": "b" * 40,
        **message,
    }
    with (
        patch("src.consumers.deploy.api_client", env.api),
        patch("src.consumers.deploy_failure_handler.api_client", env.api),
        patch("src.consumers.deploy_result_handler.api_client", env.api),
        patch("src.consumers.temporary_access_operation.api_client", env.api),
        patch("src.subgraphs.devops.deployer.api_client", env.api),
        patch("src.allocations.api_client", env.api),
        patch("src.allocations.get_settings", return_value=settings),
        patch("src.consumers.deploy.create_devops_subgraph", env.devops),
        patch("src.consumers.deploy._run_deploy_precheck", env.precheck),
        patch("src.consumers.deploy.GitHubAppClient", side_effect=env.github.client),
        patch(
            "src.consumers.temporary_access_operation.GeneratedServiceGrantClient",
            side_effect=env.product.client,
        ),
        respx.mock(assert_all_called=False) as github,
        capture_logs() as logs,
    ):
        github.route(host="api.github.com").mock(side_effect=env.github.handle)
        result = await process_deploy_job(job, env.stream)
    env.logs = logs
    return result


def proves_operation(write: dict) -> bool:
    """What the reconciler and the API accept as a grant or revoke proof."""
    result = DeployRunResult.model_validate(write["result"])
    return (
        write["status"] == RunStatus.COMPLETED.value
        and result.deploy_outcome is DeployOutcome.SUCCESS
        and result.skipped_reason is None
    )


def assert_deployed_nothing(env) -> None:
    env.devops.assert_not_called()
    env.precheck.assert_not_awaited()
    assert all(path.endswith("/cancel") for _, path in env.github.writes())
    assert env.api.other_writes == []


def assert_credential_safe(env) -> None:
    recorded = json.dumps(env.api.run_writes) + json.dumps(env.logs, default=str)
    assert CAPABILITY not in recorded


def assert_retryable_failure(env, reason: str) -> None:
    [write] = env.api.run_writes
    assert write["status"] == RunStatus.FAILED.value
    result = DeployRunResult.model_validate(write["result"])
    assert result.deploy_outcome is DeployOutcome.OWNER_ACCESS_PROOF_FAILED
    assert reason in result.error_details
    assert not proves_operation(write)
