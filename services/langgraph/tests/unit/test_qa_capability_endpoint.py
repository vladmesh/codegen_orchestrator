"""What a central QA executor can and cannot get through the capability endpoint.

The executor is a coding agent in its own container with a shell, so the
question "what may QA do to the deployment" is answered here rather than by the
shape of a Python function the agent never sees. These tests drive the endpoint
over real HTTP, the way the injected `qa` command does.

Two things are being checked. The first is that the endpoint is the same closed
set as before, argument for argument: no method parameter, no second target, no
call that was not built for this run. The second is that a container holding
everything it is given holds nothing it could use anywhere else — no SSH key, no
fleet key, no Telegram session, no provider key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import aiohttp
import pytest

from shared.qa_probe_cli import QA_PROBE_SCRIPT, QA_PROBE_USAGE
from src.agents.qa.capability_service import QACapabilityService
from src.agents.qa.tools import build_qa_callables
from src.consumers._qa_target import QACapabilities, QATarget, QATargetSession
from src.consumers._qa_workspace import qa_workspace

TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="root",
    qa_ssh_user="qa-observer",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000}),
)
CAPABILITIES = QACapabilities(
    deployed_url=TARGET.deployed_url,
    physical_root="/srv/deployments/weather-bot",
    containers=frozenset({"weather-bot-backend-1"}),
    loopback_ports=frozenset({8000}),
)
NEIGHBOUR_CONTAINER = "other-project-web-1"
PASSING_JSON = '{"pass": true, "checks": [], "summary": "OK"}'


class FakeConn:
    """A target that records everything the endpoint actually sends it."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        return SimpleNamespace(exit_status=0, stdout="", stderr="")


@pytest.fixture
async def endpoint(tmp_path):
    """A started endpoint for one run, with its workspace and its target."""
    conn = FakeConn()
    with qa_workspace(root=str(tmp_path)) as workspace:
        session = QATargetSession(TARGET, conn, CAPABILITIES)
        service = QACapabilityService(
            calls=build_qa_callables(session=session, workspace=workspace),
            capabilities=CAPABILITIES.describe(),
            submit_verdict=workspace.submit_verdict,
            advertised_host="127.0.0.1",
        )
        started = await service.start()
        try:
            yield SimpleNamespace(
                service=service,
                url=started.url,
                token=started.token,
                workspace=workspace,
                conn=conn,
            )
        finally:
            await service.stop()


async def _call(endpoint, tool: str, args: dict | None = None, *, token: str | None = None):
    headers = {"Authorization": f"Bearer {token or endpoint.token}"}
    async with aiohttp.ClientSession() as http:
        async with http.post(
            endpoint.url, json={"tool": tool, "args": args or {}}, headers=headers
        ) as response:
            return response.status, await response.json()


class TestOnlyThisRunCanUseThisEndpoint:
    async def test_a_call_without_the_run_token_is_refused(self, endpoint):
        async with aiohttp.ClientSession() as http:
            async with http.post(endpoint.url, json={"tool": "capabilities", "args": {}}) as resp:
                assert resp.status == 401

    async def test_another_runs_token_is_refused(self, endpoint):
        status, _ = await _call(
            endpoint,
            "capabilities",
            token="some-other-run",  # noqa: S106 — a test fixture, not a credential
        )

        assert status == 401

    async def test_the_endpoint_stops_answering_when_the_run_ends(self, tmp_path):
        conn = FakeConn()
        with qa_workspace(root=str(tmp_path)) as workspace:
            service = QACapabilityService(
                calls=build_qa_callables(
                    session=QATargetSession(TARGET, conn, CAPABILITIES), workspace=workspace
                ),
                capabilities=CAPABILITIES.describe(),
                submit_verdict=workspace.submit_verdict,
                advertised_host="127.0.0.1",
            )
            started = await service.start()
            await service.stop()

        with pytest.raises(aiohttp.ClientError):
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    started.url,
                    json={"tool": "capabilities", "args": {}},
                    headers={"Authorization": f"Bearer {started.token}"},
                ):
                    pass


class TestTheSetIsClosed:
    async def test_capabilities_names_exactly_what_this_run_may_reach(self, endpoint):
        _, body = await _call(endpoint, "capabilities")

        assert body["deployed_url"] == TARGET.deployed_url
        assert body["containers"] == ["weather-bot-backend-1"]
        assert body["loopback_ports"] == [8000]

    @pytest.mark.parametrize(
        "tool",
        ["http_post", "remote_write", "exec", "ssh", "docker", "read_file"],
    )
    async def test_a_call_that_does_not_exist_is_refused(self, endpoint, tool):
        status, body = await _call(endpoint, tool)

        assert status == 400
        assert "is not a call this run has" in body["error"]

    async def test_an_argument_the_call_does_not_declare_is_refused(self, endpoint):
        """No smuggling a method, a host or a body past a typed call."""
        status, body = await _call(endpoint, "http_get", {"path": "/x", "method": "POST"})

        assert status == 400
        assert "http_get" in body["error"]
        assert endpoint.conn.commands == []

    async def test_a_neighbours_container_is_refused_by_the_capability_set(self, endpoint):
        _, body = await _call(endpoint, "container_logs", {"container": NEIGHBOUR_CONTAINER})

        assert "is not a container of this run's deployment" in body["error"]
        assert endpoint.conn.commands == []

    async def test_a_port_that_is_not_this_deployments_is_refused(self, endpoint):
        _, body = await _call(endpoint, "localhost_http_get", {"port": 9000, "path": "/private"})

        assert "not allocated to this run's deployment" in body["error"]

    async def test_an_allowed_read_reaches_the_target_as_a_get(self, endpoint):
        await _call(endpoint, "localhost_http_get", {"port": 8000, "path": "/health"})

        [command] = endpoint.conn.commands
        assert "--get" in command
        assert "-X" not in command

    async def test_every_served_call_is_recorded_by_the_runner(self, endpoint):
        await _call(endpoint, "container_logs", {"container": "weather-bot-backend-1"})

        trace = endpoint.workspace.trace_path.read_text()
        assert '"tool": "container_logs"' in trace


class TestTheVerdictComesBackThroughTheEndpoint:
    async def test_a_submitted_verdict_is_stored_and_ends_the_run(self, endpoint):
        status, body = await _call(endpoint, "submit_qa_result", {"result": PASSING_JSON})

        assert status == 200
        assert endpoint.workspace.verdict == PASSING_JSON
        assert endpoint.service.verdict_received.is_set()
        assert "recorded" in body["result"]

    async def test_an_empty_verdict_is_not_a_verdict(self, endpoint):
        status, _ = await _call(endpoint, "submit_qa_result", {"result": "   "})

        assert status == 400
        assert endpoint.service.verdict_received.is_set() is False

    async def test_served_calls_are_counted_so_a_silent_run_is_distinguishable(self, endpoint):
        assert endpoint.service.calls_served == 0

        await _call(endpoint, "container_logs", {"container": "weather-bot-backend-1"})

        assert endpoint.service.calls_served == 1


class TestTheExecutorHoldsNoCredential:
    """AC6, from the container's side: what it is given is all it has."""

    async def test_the_endpoint_never_returns_a_credential(self, endpoint):
        _, body = await _call(endpoint, "capabilities")

        blob = json.dumps(body)
        for secret in ("BEGIN OPENSSH PRIVATE KEY", "TELETHON", "api_key", "ANTHROPIC"):
            assert secret not in blob

    def test_the_injected_command_carries_no_address_and_no_secret(self):
        """It reads both from the environment, so a copy of it is worth nothing."""
        assert "QA_CAPABILITY_URL" in QA_PROBE_SCRIPT
        assert "QA_CAPABILITY_TOKEN" in QA_PROBE_SCRIPT
        for secret in ("ANTHROPIC", "TELETHON", "ssh", "PRIVATE KEY"):
            assert secret not in QA_PROBE_SCRIPT

    def test_the_command_and_the_prompt_cannot_describe_different_calls(self):
        from src.prompts.qa import build_qa_prompt

        prompt = build_qa_prompt(
            "- GET /health returns 200",
            TARGET.deployed_url,
        )

        assert QA_PROBE_USAGE in prompt
