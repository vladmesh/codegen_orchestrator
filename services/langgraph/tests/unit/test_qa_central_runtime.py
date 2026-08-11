"""The central QA runtime: a clean target, one target, and nothing left behind.

These tests drive `run_qa_centrally` with a fake SSH layer that answers only the
commands the runner actually sends. That is the point: the target in these tests
has no Claude CLI, no LLM credentials and no Telethon session, and a run that
needed any of them would have to ask for it here, where every command is
recorded.
"""

from __future__ import annotations

import re
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import (
    QAGrantError,
    QAGrantOutcome,
    QATarget,
    QATargetError,
    QATargetSession,
    new_grant_marker,
    qa_target_grant,
)
from src.consumers._qa_workspace import qa_workspace

TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="deploy",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
)
RUNTIME = QARuntimeConfig(model="m", base_url="http://llm.invalid/v1", api_key="k")
PASSING_JSON = (
    '{"pass": true, "checks": [{"name": "health", "pass": true, "detail": "200"}], "summary": "OK"}'
)

# Anything a target would need if the agent still lived on it. A command
# mentioning one of these is the regression this card exists to prevent.
ON_TARGET_AGENT_MARKERS = (
    "claude",
    ".credentials.json",
    "qa-telethon",
    "install.sh",
    "ANTHROPIC_API_KEY",
    "qa-write-guard",
)


class FakeConn:
    """A target that answers the runner's typed commands and records them all."""

    def __init__(self, responses: dict[str, SimpleNamespace] | None = None) -> None:
        self.commands: list[str] = []
        self.responses = responses or {}
        self.authorized_keys: list[str] = []
        self.installed: list[str] = []
        self.closed = False

    @staticmethod
    def _flocked_shell(command: str) -> list[str] | None:
        """The argv of the shell the runner wrapped in flock, if it wrapped one."""
        if "flock " not in command:
            return None
        tokens = shlex.split(command)
        return shlex.split(tokens[tokens.index("-c") + 1])

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        inner = self._flocked_shell(command)
        if inner and inner[0] == "printf":
            self.authorized_keys.append(inner[2])
            self.installed.append(inner[2])
            return SimpleNamespace(exit_status=0, stdout="", stderr="")
        if inner and inner[0] == "grep":
            marker = inner[inner.index("-F") + 1]
            self.authorized_keys = [k for k in self.authorized_keys if marker not in k]
            return SimpleNamespace(exit_status=0, stdout="", stderr="")
        if command.startswith("grep -c -F"):
            marker = shlex.split(command)[3]
            hits = sum(1 for k in self.authorized_keys if marker in k)
            return SimpleNamespace(exit_status=0, stdout=f"{hits}\n", stderr="")
        for needle, response in self.responses.items():
            if needle in command:
                return response
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        return False


class FakeGraph:
    """Stands in for the compiled ReactAgent; drives the tools it was built with."""

    def __init__(self, tools, behaviour):
        self.tools = {tool.name: tool for tool in tools}
        self._behaviour = behaviour
        self.tool_results: list = []

    async def ainvoke(self, state, config=None):
        content = await self._behaviour(self)
        return {"messages": [SimpleNamespace(content=content)]}


def _graph_factory(behaviour):
    def create(*, model, base_url, api_key, tools, prompt):
        create.graph = FakeGraph(tools, behaviour)
        create.prompt = prompt
        return create.graph

    return create


@pytest.fixture
def central_run(tmp_path):
    """Run `run_qa_centrally` against a fake target with a scripted agent."""

    async def _run(*, behaviour, conn=None, target=TARGET):
        connection = conn or FakeConn()
        factory = _graph_factory(behaviour)
        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=connection)),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.create_qa_graph", factory),
        ):
            result = await run_qa_centrally(
                target=target,
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
            )
        return result, connection, factory

    return _run


class TestCleanTargetPassesExploratoryQA:
    async def test_clean_target_needs_no_agent_runtime(self, central_run):
        """A target with no Claude, no LLM key and no Telethon session passes QA."""

        async def behaviour(graph):
            await graph.tools["write_qa_report"].ainvoke({"markdown": "# QA Report\nall good"})
            return PASSING_JSON

        result, conn, _ = await central_run(behaviour=behaviour)

        assert result.passed is True
        assert result.blocker is None
        assert result.report == "# QA Report\nall good"
        for command in conn.commands:
            for marker in ON_TARGET_AGENT_MARKERS:
                assert marker not in command, f"{marker!r} was still asked of the target: {command}"

    async def test_the_run_uses_its_own_identity_not_the_fleet_key(self, tmp_path):
        """The fleet key installs and removes a key; it is not what QA connects with."""
        captured = {}

        async def behaviour(graph):
            return PASSING_JSON

        conn = FakeConn()

        async def connect(server_ip, ssh_user, key):
            captured.setdefault("keys", []).append(key)
            return conn

        factory = _graph_factory(behaviour)
        with (
            patch("src.consumers._qa_target._connect", connect),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.create_qa_graph", factory),
        ):
            await run_qa_centrally(
                target=TARGET,
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
            )

        admin_key, run_key, revoke_key = captured["keys"]
        assert admin_key == "fleet-key"
        assert revoke_key == "fleet-key"
        assert run_key != "fleet-key"
        assert run_key.get_algorithm() == "ssh-ed25519"

    async def test_the_granted_key_is_restricted_and_expires(self, central_run):
        async def behaviour(graph):
            return PASSING_JSON

        _, conn, _ = await central_run(behaviour=behaviour)

        [entry] = conn.installed
        assert "restrict," in entry
        assert re.search(r'expiry-time="\d{12}"', entry)


class TestGrantAndWorkspaceAreDestroyed:
    async def test_a_failed_run_still_revokes_and_removes(self, central_run):
        async def behaviour(graph):
            raise RuntimeError("the agent died mid-run")

        result, conn, _ = await central_run(behaviour=behaviour)

        assert result.passed is False
        assert conn.authorized_keys == []
        assert result.blocker.category is QABlockerCategory.UNKNOWN

    async def test_a_cancelled_run_still_revokes(self, tmp_path):
        conn = FakeConn()
        outcome = QAGrantOutcome(marker=new_grant_marker())

        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
            patch("src.consumers._qa_target._import", lambda key: key),
        ):
            with pytest.raises(RuntimeError):
                async with qa_target_grant(
                    target=TARGET, fleet_ssh_key="fleet-key", outcome=outcome
                ):
                    assert conn.authorized_keys
                    raise RuntimeError("cancelled")

        assert outcome.revoked is True
        assert conn.authorized_keys == []

    async def test_workspace_is_gone_after_a_raising_run(self, tmp_path):
        with pytest.raises(RuntimeError):
            with qa_workspace(root=str(tmp_path)) as workspace:
                path = workspace.path
                workspace.write_report("partial")
                raise RuntimeError("boom")

        assert not path.exists()
        assert workspace.destroyed is True

    async def test_surviving_access_blocks_an_otherwise_passing_run(self, central_run):
        """A key that outlived the run is not a green QA run."""

        class StubbornConn(FakeConn):
            async def run(self, command, *, check=False, timeout=None):
                if command.startswith("grep -c -F"):
                    self.commands.append(command)
                    return SimpleNamespace(exit_status=0, stdout="1\n", stderr="")
                return await super().run(command, check=check, timeout=timeout)

        async def behaviour(graph):
            return PASSING_JSON

        result, _, _ = await central_run(behaviour=behaviour, conn=StubbornConn())

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert result.state_changes[0]["cleanup"]["succeeded"] is False

    async def test_a_target_that_refuses_the_identity_blocks_the_run(self, tmp_path):
        async def behaviour(graph):
            return PASSING_JSON

        with (
            patch(
                "src.consumers._qa_target._connect",
                AsyncMock(side_effect=OSError("connection refused")),
            ),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.create_qa_graph", _graph_factory(behaviour)),
        ):
            result = await run_qa_centrally(
                target=TARGET,
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
            )

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.SERVER_UNAVAILABLE


class TestOneTargetOnly:
    def _session(self, conn=None):
        return QATargetSession(TARGET, conn or FakeConn())

    def test_a_path_outside_the_deployment_is_refused(self):
        session = self._session()

        with pytest.raises(QATargetError):
            session.resolve_path("/opt/services/other-project/compose.yml")

    def test_traversal_out_of_the_deployment_is_refused(self):
        session = self._session()

        with pytest.raises(QATargetError):
            session.resolve_path("../other-project/compose.yml")

    def test_relative_paths_land_inside_the_deployment(self):
        session = self._session()

        assert session.resolve_path("infra/compose.yml") == (
            "/opt/services/weather-bot/infra/compose.yml"
        )

    @pytest.mark.parametrize(
        "path",
        [".env", "infra/.env.prod", "keys/deploy.pem", "credentials.json"],
    )
    def test_deployment_credentials_are_refused(self, path):
        session = self._session()

        with pytest.raises(QATargetError):
            session.resolve_path(path)

    def test_another_projects_container_is_refused(self):
        session = self._session()

        with pytest.raises(QATargetError):
            session.resolve_container("other-project-backend-1")

    def test_own_container_is_accepted(self):
        session = self._session()

        assert session.resolve_container("weather-bot-backend-1") == "weather-bot-backend-1"

    async def test_tools_carry_the_refusal_back_to_the_agent(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool
                for tool in build_qa_tools(session=self._session(), workspace=workspace)
            }
            answer = await tools["container_logs"].ainvoke({"container": "other-project-web-1"})

        assert "does not belong to weather-bot" in answer["error"]


class TestTheAgentHasNoShell:
    def _session(self, conn=None):
        return QATargetSession(TARGET, conn or FakeConn())

    @pytest.mark.parametrize(
        "argv",
        [
            ["curl", "-X", "POST", "http://1.2.3.4:8000/users"],
            ["python3", "-c", "import httpx"],
            ["rm", "-rf", "/opt/services/weather-bot"],
            ["docker", "exec", "weather-bot-backend-1", "sh"],
            ["docker", "compose", "restart"],
            ["systemctl", "restart", "docker"],
            ["sh", "-c", "curl -d {} http://1.2.3.4:8000/users"],
            ["cat", "/etc/shadow"],
            # These name a container, so they must go through the tools that
            # check the name belongs to this run — not through exec.
            ["docker", "logs", "other-project-web-1"],
            ["docker", "inspect", "other-project-web-1"],
            # A global flag before the sub-command must not launder a write.
            ["docker", "--context", "ps", "rm", "weather-bot-backend-1"],
        ],
    )
    async def test_write_capable_commands_are_refused(self, argv):
        session = self._session()

        with pytest.raises(QATargetError):
            await session.exec(argv)

    async def test_read_only_commands_are_allowed(self):
        conn = FakeConn()
        session = self._session(conn)

        await session.exec(["docker", "ps", "-a"])

        assert conn.commands == ["docker ps -a"]

    async def test_arguments_cannot_smuggle_a_shell(self):
        conn = FakeConn()
        session = self._session(conn)

        await session.exec(["docker", "ps", "--filter", "name=a b; rm -rf /"])

        assert conn.commands == ["docker ps --filter 'name=a b; rm -rf /'"]

    async def test_the_localhost_probe_can_only_get(self):
        conn = FakeConn()
        session = self._session(conn)

        await session.localhost_http_get(8000, "/health")

        [command] = conn.commands
        assert "--get" in command
        assert "http://127.0.0.1:8000/health" in command
        assert "-X" not in command

    @pytest.mark.parametrize("path", ["health", "/hea lth"])
    async def test_the_localhost_probe_refuses_a_non_path(self, path):
        session = self._session()

        with pytest.raises(QATargetError):
            await session.localhost_http_get(8000, path)


class TestWriteGuard:
    async def test_a_write_in_the_runner_trace_blocks_the_run(self, central_run):
        """The runner owns the trace, so a write it can see fails the run closed."""

        async def behaviour(graph):
            return (
                '{"pass": true, "checks": [{"name": "signup", "pass": true, '
                '"detail": "POST http://1.2.3.4:8000/users returned 201"}], "summary": "OK"}'
            )

        result, _, factory = await central_run(behaviour=behaviour)

        assert result.passed is False
        assert result.summary == "QA attempted a forbidden application API write"
        assert result.state_changes[0]["resource"] == "POST http://1.2.3.4:8000/users"

    async def test_the_trace_is_written_by_the_runner_not_the_agent(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        session = QATargetSession(TARGET, FakeConn())
        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=session, workspace=workspace)
            }
            await tools["remote_exec"].ainvoke({"command": ["docker", "ps"]})
            trace = workspace.trace_path.read_text()

        assert '"tool": "remote_exec"' in trace
        assert "docker ps" in trace


class TestGrantFailureIsNamed:
    async def test_a_target_that_refuses_the_key_install_is_a_blocker(self, tmp_path):
        conn = FakeConn()

        async def failing_run(command, *, check=False, timeout=None):
            return SimpleNamespace(exit_status=1, stdout="", stderr="Permission denied")

        conn.run = failing_run
        outcome = QAGrantOutcome(marker=new_grant_marker())

        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
            patch("src.consumers._qa_target._import", lambda key: key),
        ):
            with pytest.raises(QAGrantError) as exc:
                async with qa_target_grant(
                    target=TARGET, fleet_ssh_key="fleet-key", outcome=outcome
                ):
                    pass

        assert "Permission denied" in str(exc.value)
