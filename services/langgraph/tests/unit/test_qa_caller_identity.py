"""Central QA reads identity-bearing package routes as one verified QA user.

Kit core 2.1 takes a package route's owner from the verified caller: the
product's `USER_IDENTITY_CAPABILITY` in `X-Identity-Capability`, and an active
user in `X-User-Channel` and `X-User-External-Id`. These tests drive the one
boundary — `build_qa_callables` — and the one grant construction place —
`resolve_qa_caller_identity` — and read back everything the run records.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from structlog.testing import capture_logs

from shared.contracts.acceptance import parse_scheduled_behaviours
from src.agents.qa.caller_identity import (
    QA_PLATFORM_USER_REF,
    QACallerIdentity,
    caller_identity_facts,
    caller_identity_record,
    resolve_qa_caller_identity,
)
from src.agents.qa.capability_service import QACapabilityService
from src.agents.qa.packages import ActivePackage, PackageActivation, behaviour_check_name
from src.agents.qa.tools import QAJobsCapability, build_qa_callables
from src.clients.product_jobs import GeneratedServiceJobsClient
from src.clients.users_grant import GeneratedServiceGrantClient
from src.consumers._qa_runner import (
    QAResult,
    apply_package_acceptance,
    run_package_acceptance_checks,
)
from src.consumers._qa_target import STATUS_MARKER, QACapabilities, QATarget, QATargetSession
from src.consumers._qa_workspace import QAWorkspace
from src.prompts.qa import build_qa_prompt

DEPLOYED_URL = "https://reminders.example.com"
IDENTITY_CAPABILITY = "identity-capability-only-in-a-header"  # noqa: S105
GRANT_CAPABILITY = "grant-capability-only-in-a-header"  # noqa: S105
SECRETS = {
    "USER_IDENTITY_CAPABILITY": IDENTITY_CAPABILITY,
    "USERS_GRANT_CAPABILITY": GRANT_CAPABILITY,
}
IDENTITY = QACallerIdentity("qa", "central-qa", IDENTITY_CAPABILITY)
CRITERIA = (
    '- FIRE JOB reminders.tick WITH {"at": "2999-01-01T00:00:00Z"} THEN GET /reminders '
    "shows the reminder as emitted\n"
)
CAPABILITIES = QACapabilities(
    deployed_url=DEPLOYED_URL,
    physical_root="/srv/deployments/reminders",
    containers=frozenset({"reminders-backend-1"}),
    loopback_ports=frozenset({8000}),
)
IDENTITY_HEADERS = ("X-Identity-Capability", "X-User-Channel", "X-User-External-Id")


def _workspace(tmp_path: Path) -> QAWorkspace:
    workspace = QAWorkspace(path=tmp_path)
    workspace.trace_path.touch()
    return workspace


class _RecordingConn:
    """A target that records every command it is sent and answers 200."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        return SimpleNamespace(exit_status=0, stdout=f"[]\n{STATUS_MARKER}200>>", stderr="")


def _session(conn: _RecordingConn | None = None) -> QATargetSession:
    target = QATarget(
        server_ip="1.2.3.4",
        ssh_user="root",
        qa_ssh_user="qa-observer",
        server_handle="vps-1",
        project_name="reminders",
        deployed_url=DEPLOYED_URL,
        allocated_ports=frozenset({8000}),
    )
    return QATargetSession(target, conn or _RecordingConn(), CAPABILITIES)


def _product(handler) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """The deployed product's public surface, recording every request it receives."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(respond), seen


def _echo(status: int):
    """A product that answers with everything the request carried — the worst case."""

    def handler(request: httpx.Request) -> httpx.Response:
        echoed = dict(request.headers)
        return httpx.Response(status, json=echoed, headers={"X-Echo": json.dumps(echoed)})

    return handler


def _users_core(access_status: str = "active") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/users/grant":
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "user_id": 1,
                "status": access_status,
                "channel": request.url.params["channel"],
                "external_id": request.url.params["external_id"],
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestHttpGetCarriesTheIdentity:
    """AC2 — the runtime-side read sends the headers; nothing else does."""

    @pytest.mark.asyncio
    async def test_with_an_identity_each_header_is_sent_exactly_once(self, tmp_path):
        transport, seen = _product(lambda request: httpx.Response(200, json=[]))
        calls = build_qa_callables(
            session=_session(),
            workspace=_workspace(tmp_path),
            caller_identity=IDENTITY,
            http_transport=transport,
        )

        await calls["http_get"]("/reminders")

        [request] = seen
        assert request.headers.get_list("X-Identity-Capability") == [IDENTITY_CAPABILITY]
        assert request.headers.get_list("X-User-Channel") == ["qa"]
        assert request.headers.get_list("X-User-External-Id") == ["central-qa"]
        assert request.url == f"{DEPLOYED_URL}/reminders"
        assert "user_ref" not in str(request.url)

    @pytest.mark.asyncio
    async def test_without_an_identity_none_of_them_is_sent(self, tmp_path):
        transport, seen = _product(lambda request: httpx.Response(200, json=[]))
        calls = build_qa_callables(
            session=_session(), workspace=_workspace(tmp_path), http_transport=transport
        )

        await calls["http_get"]("/reminders")

        [request] = seen
        assert not any(name in request.headers for name in IDENTITY_HEADERS)

    @pytest.mark.asyncio
    async def test_localhost_http_get_never_sends_the_capability(self, tmp_path):
        """A curl on the target would put the capability in the target's argv."""
        conn = _RecordingConn()
        calls = build_qa_callables(
            session=_session(conn), workspace=_workspace(tmp_path), caller_identity=IDENTITY
        )

        await calls["localhost_http_get"](8000, "/reminders")

        [command] = conn.commands
        assert "127.0.0.1:8000/reminders" in command
        assert IDENTITY_CAPABILITY not in command
        assert not any(name in command for name in IDENTITY_HEADERS)


class TestNeitherCapabilityIsInAnythingTheRunRecords:
    """AC3 — after a grant and identity-bearing reads, every artifact is clean."""

    @pytest.mark.asyncio
    async def test_recorded_requests_observations_trace_facts_and_errors(self, tmp_path):
        workspace = _workspace(tmp_path)
        with capture_logs() as logs:
            identity, blocker = await resolve_qa_caller_identity(
                deployed_url=DEPLOYED_URL,
                secrets=SECRETS,
                telegram_account_id=None,
                grant_client_factory=lambda url: GeneratedServiceGrantClient(
                    url, transport=_users_core()
                ),
            )
            assert blocker is None
            assert identity is not None
            # A grant the product does not prove, and one that never arrives.
            _, inactive = await resolve_qa_caller_identity(
                deployed_url=DEPLOYED_URL,
                secrets=SECRETS,
                telegram_account_id=None,
                grant_client_factory=lambda url: GeneratedServiceGrantClient(
                    url, transport=_users_core("inactive")
                ),
            )
            unreachable = AsyncMock()
            unreachable.request.side_effect = httpx.ConnectError(
                f"refused while sending {GRANT_CAPABILITY}"
            )
            _, transport_failed = await resolve_qa_caller_identity(
                deployed_url=DEPLOYED_URL,
                secrets=SECRETS,
                telegram_account_id=None,
                grant_client_factory=lambda url: GeneratedServiceGrantClient(
                    url, transport=unreachable
                ),
            )

            def product(request: httpx.Request) -> httpx.Response:
                if request.url.path == "/down":
                    # A transport error whose text carries the request's headers.
                    raise httpx.ConnectError(f"reset after {dict(request.headers)}")
                return _echo(500 if request.url.path == "/broken" else 200)(request)

            transport, seen = _product(product)
            calls = build_qa_callables(
                session=_session(),
                workspace=workspace,
                caller_identity=identity,
                http_transport=transport,
            )
            service = QACapabilityService(
                calls=calls,
                capabilities=CAPABILITIES.describe(),
                submit_verdict=lambda raw: None,
                advertised_host="127.0.0.1",
            )
            answers = [
                await service._dispatch("http_get", {"path": "/reminders"}),
                await calls["http_get"]("/broken"),
                await calls["http_get"]("/down"),
                await calls["localhost_http_get"](8000, "/reminders"),
            ]

        # Every read really carried the identity, so the scrub had something to find.
        assert len(seen) == 3
        assert all(one.headers["X-Identity-Capability"] == IDENTITY_CAPABILITY for one in seen)
        assert answers[1]["status"] == 500
        assert "transport error" in answers[2]["error"]
        facts = caller_identity_facts(identity)
        evidence = {
            "trace": workspace.trace_text(),
            "trace file": workspace.trace_path.read_text(),
            "observations": repr(workspace.observations),
            "answers to the executor": json.dumps(answers),
            "facts": "\n".join(facts),
            "prompt": build_qa_prompt(CRITERIA, DEPLOYED_URL, established_facts=facts),
            "run metadata": json.dumps(caller_identity_record(identity)),
            "identity repr": repr(identity),
            "blockers": json.dumps(
                [inactive.model_dump(mode="json"), transport_failed.model_dump(mode="json")]
            ),
            "logs": repr(logs),
        }
        for where, text in evidence.items():
            assert IDENTITY_CAPABILITY not in text, where
            assert GRANT_CAPABILITY not in text, where
        assert QA_PLATFORM_USER_REF in evidence["facts"]
        assert "/reminders" in evidence["observations"]
        assert inactive.received.startswith("inactive:")
        assert transport_failed.received.startswith("transport:")


class TestAPackageBehaviourBindsOnAnIdentityBearingRead:
    """AC4 — the `observation_answers` rule, on `GET /reminders` with no `user_ref`."""

    PACKAGE = ActivePackage("reminders", "0.4.0", "f" * 64)
    ROW = behaviour_check_name("reminders", "reminders.tick")
    JUDGED = {
        "name": "reminders.tick emits the seeded reminder",
        "pass": True,
        "detail": "after the fire, GET /reminders showed the reminder in state emitted",
    }

    def _calls(self, tmp_path: Path, product) -> tuple[dict, QAWorkspace]:
        behaviours = tuple(parse_scheduled_behaviours(CRITERIA))
        jobs = AsyncMock()
        jobs.request.return_value = httpx.Response(
            200,
            json={
                "contract_version": 1,
                "command_id": "qa-qa-run-1-reminders.tick",
                "name": "reminders.tick",
                "arguments": {"at": "2999-01-01T00:00:00Z"},
                "fired_by_product": "project-1",
                "fired_by_run": "qa-run-1",
                "dispatch_status": "dispatched",
                "accepted_at": "2026-10-02T10:00:00Z",
                "dispatched_at": "2026-10-02T10:00:01Z",
            },
            request=httpx.Request("POST", f"{DEPLOYED_URL}/jobs/fire"),
        )
        workspace = _workspace(tmp_path)
        transport, _ = _product(product)
        calls = build_qa_callables(
            session=_session(),
            workspace=workspace,
            jobs=QAJobsCapability(
                base_url=DEPLOYED_URL,
                capability="jobs-capability",
                fired_by_product="project-1",
                fired_by_run="qa-run-1",
                behaviours=behaviours,
            ),
            jobs_client_factory=lambda url: GeneratedServiceJobsClient(url, transport=jobs),
            caller_identity=IDENTITY,
            http_transport=transport,
        )
        return calls, workspace

    def _settle(self, workspace: QAWorkspace) -> QAResult:
        acceptance = run_package_acceptance_checks(
            PackageActivation(
                packages=(self.PACKAGE,),
                listed=("reminders",),
                jobs={"reminders.tick": "package:reminders"},
            ),
            criteria_behaviours=parse_scheduled_behaviours(CRITERIA),
        )
        return apply_package_acceptance(
            QAResult(passed=True, checks=[self.JUDGED], summary="OK"), acceptance, workspace
        )

    @staticmethod
    def _reminders(request: httpx.Request) -> httpx.Response:
        if request.headers.get("X-Identity-Capability") != IDENTITY_CAPABILITY:
            return httpx.Response(401, json={"detail": "Caller identity capability required"})
        return httpx.Response(
            200, json=[{"user_ref": QA_PLATFORM_USER_REF, "state": "emitted", "text": "hi"}]
        )

    @pytest.mark.asyncio
    async def test_the_row_passes_on_a_read_of_get_reminders_as_the_qa_user(self, tmp_path):
        calls, workspace = self._calls(tmp_path, self._reminders)

        await calls["fire_job"]("reminders.tick")
        answer = await calls["http_get"]("/reminders")
        result = self._settle(workspace)

        assert answer["status"] == 200
        row = next(check for check in result.checks if check["name"] == self.ROW)
        assert row["pass"] is True, row
        assert "http_get /reminders" in row["detail"]
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_the_row_still_fails_when_no_read_of_the_route_succeeded(self, tmp_path):
        """The product refused the read: a 401 or 403 is not the product's output."""
        calls, workspace = self._calls(
            tmp_path, lambda request: httpx.Response(403, json={"detail": "Caller is not active"})
        )

        await calls["fire_job"]("reminders.tick")
        answer = await calls["http_get"]("/reminders")
        result = self._settle(workspace)

        assert answer["status"] == 403
        row = next(check for check in result.checks if check["name"] == self.ROW)
        assert row["pass"] is False
        assert "made no successful read of /reminders" in row["detail"]
        assert result.passed is False
