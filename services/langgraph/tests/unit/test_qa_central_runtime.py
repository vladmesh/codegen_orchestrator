"""The central QA runtime: a clean target, one deployment, and nothing left behind.

These tests drive `run_qa_centrally` with a fake SSH layer that answers only the
commands the runner actually sends. That is the point: the target in these tests
has no Claude CLI, no LLM credentials and no Telethon session, and a run that
needed any of them would have to ask for it here, where every command is
recorded.

The other half is the capability set. A run's boundary is one object resolved
from deployment data — physical root, containers, loopback ports, public URL —
and every refusal below is a membership test against it. Where a second project
shares the host, that is what has to refuse.
"""

from __future__ import annotations

import re
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.qa_ssh_grant import QASshGrantState
from shared.contracts.dto.run_result import QABlockerCategory
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import (
    QACapabilities,
    QACapabilityError,
    QAGrantError,
    QAGrantOutcome,
    QATarget,
    QATargetError,
    QATargetSession,
    new_grant_marker,
    qa_target_grant,
    resolve_capabilities,
)
from src.consumers._qa_workspace import qa_workspace

TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="deploy",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000, 8001}),
)
# What the target reports back about this deployment. Another project's
# containers and ports exist on the same host and are absent from it.
OWN_CONTAINERS = ("weather-bot-backend-1", "weather-bot-db-1")
OTHER_PROJECT_CONTAINER = "other-project-web-1"
OTHER_PROJECT_PORT = 9000
PHYSICAL_ROOT = "/srv/deployments/weather-bot"

RUNTIME = QARuntimeConfig(model="m", base_url="http://llm.invalid/v1", api_key="k")
PASSING_JSON = (
    '{"pass": true, "checks": [{"name": "health", "pass": true, "detail": "200"}], "summary": "OK"}'
)
CAPABILITIES = QACapabilities(
    deployed_url=TARGET.deployed_url,
    physical_root=PHYSICAL_ROOT,
    containers=frozenset(OWN_CONTAINERS),
    loopback_ports=frozenset({8000, 8001}),
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


class RecordingJournal:
    """Stands in for the durable record the run's grant is written to."""

    def __init__(self) -> None:
        self.states: list[QASshGrantState] = []
        self.grants: list = []

    async def write(self, grant) -> None:
        self.states.append(grant.state)
        self.grants.append(grant)


class FakeConn:
    """A target that answers the runner's typed commands and records them all."""

    def __init__(self, containers: tuple[str, ...] = OWN_CONTAINERS) -> None:
        self.commands: list[str] = []
        self.authorized_keys: list[str] = []
        self.installed: list[str] = []
        self.containers = containers
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
        if command.startswith("readlink -f --"):
            return SimpleNamespace(exit_status=0, stdout=f"{PHYSICAL_ROOT}\n", stderr="")
        if command.startswith("docker ps"):
            return SimpleNamespace(
                exit_status=0, stdout="".join(f"{name}\n" for name in self.containers), stderr=""
            )
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

    async def ainvoke(self, state, config=None):
        return {"messages": [SimpleNamespace(content=await self._behaviour(self))]}


def _graph_factory(behaviour):
    def create(*, model, base_url, api_key, tools, prompt):
        create.graph = FakeGraph(tools, behaviour)
        create.prompt = prompt
        return create.graph

    return create


def _session(conn=None, capabilities: QACapabilities = CAPABILITIES) -> QATargetSession:
    return QATargetSession(TARGET, conn or FakeConn(), capabilities)


@pytest.fixture
def central_run(tmp_path):
    """Run `run_qa_centrally` against a fake target with a scripted agent."""

    async def _run(*, behaviour, conn=None, target=TARGET, journal=None):
        connection = conn or FakeConn()
        factory = _graph_factory(behaviour)
        record = journal or RecordingJournal()
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
                grant_journal=record,
            )
        return result, connection, factory, record

    return _run


class TestCleanTargetPassesExploratoryQA:
    async def test_clean_target_needs_no_agent_runtime(self, central_run):
        """A target with no Claude, no LLM key and no Telethon session passes QA."""

        async def behaviour(graph):
            await graph.tools["write_qa_report"].ainvoke({"markdown": "# QA Report\nall good"})
            return PASSING_JSON

        result, conn, _, _ = await central_run(behaviour=behaviour)

        assert result.passed is True
        assert result.blocker is None
        assert result.report == "# QA Report\nall good"
        for command in conn.commands:
            for marker in ON_TARGET_AGENT_MARKERS:
                assert marker not in command, f"{marker!r} was still asked of the target: {command}"

    async def test_the_run_uses_its_own_identity_not_the_fleet_key(self, tmp_path):
        """The fleet key installs and removes a key; it is not what QA connects with."""
        captured: list = []
        conn = FakeConn()

        async def connect(server_ip, ssh_user, key):
            captured.append(key)
            return conn

        async def behaviour(graph):
            return PASSING_JSON

        with (
            patch("src.consumers._qa_target._connect", connect),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.create_qa_graph", _graph_factory(behaviour)),
        ):
            await run_qa_centrally(
                target=TARGET,
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
                grant_journal=RecordingJournal(),
            )

        admin_key, run_key, revoke_key = captured
        assert admin_key == "fleet-key"
        assert revoke_key == "fleet-key"
        assert run_key != "fleet-key"
        assert run_key.get_algorithm() == "ssh-ed25519"

    async def test_the_granted_key_is_restricted_and_expires(self, central_run):
        async def behaviour(graph):
            return PASSING_JSON

        _, conn, _, _ = await central_run(behaviour=behaviour)

        [entry] = conn.installed
        assert "restrict," in entry
        assert re.search(r'expiry-time="\d{12}"', entry)


class TestCapabilitySet:
    """The set is resolved from the deployment, not guessed by a tool."""

    async def test_root_is_what_the_target_resolves_and_containers_are_what_docker_says(self):
        conn = FakeConn()

        capabilities = await resolve_capabilities(conn, TARGET)

        assert capabilities.physical_root == PHYSICAL_ROOT
        assert capabilities.containers == frozenset(OWN_CONTAINERS)
        assert capabilities.loopback_ports == frozenset({8000, 8001})
        assert capabilities.deployed_url == TARGET.deployed_url
        listing = next(c for c in conn.commands if c.startswith("docker ps"))
        # The compose project label is what makes this the deployment's own list
        # rather than a name-prefix guess about the host's containers.
        assert "label=com.docker.compose.project=weather-bot" in listing

    async def test_a_deployment_directory_that_does_not_resolve_is_refused(self):
        conn = FakeConn()

        async def run(command, *, check=False, timeout=None):
            if command.startswith("readlink"):
                return SimpleNamespace(exit_status=1, stdout="", stderr="No such file")
            return SimpleNamespace(exit_status=0, stdout="", stderr="")

        conn.run = run

        with pytest.raises(QACapabilityError):
            await resolve_capabilities(conn, TARGET)

    async def test_a_run_that_cannot_resolve_its_capabilities_is_blocked(self, central_run):
        class NoDeployment(FakeConn):
            async def run(self, command, *, check=False, timeout=None):
                if command.startswith("readlink"):
                    self.commands.append(command)
                    return SimpleNamespace(exit_status=1, stdout="", stderr="No such file")
                return await super().run(command, check=check, timeout=timeout)

        async def behaviour(graph):
            return PASSING_JSON

        result, conn, _, _ = await central_run(behaviour=behaviour, conn=NoDeployment())

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.SERVER_UNAVAILABLE
        # The grant is still taken back: the run never started, the key did.
        assert conn.authorized_keys == []


class TestASecondProjectOnTheSameHost:
    """Every boundary, checked against a neighbour that shares the machine."""

    def test_another_projects_container_is_refused(self):
        session = _session()

        with pytest.raises(QATargetError) as exc:
            session.check_container(OTHER_PROJECT_CONTAINER)

        assert OTHER_PROJECT_CONTAINER in str(exc.value)

    async def test_docker_exec_cannot_name_another_projects_container(self):
        session = _session()

        with pytest.raises(QATargetError):
            await session.exec(["docker", "logs", OTHER_PROJECT_CONTAINER])

    async def test_another_projects_loopback_port_is_refused(self):
        session = _session()

        with pytest.raises(QATargetError) as exc:
            await session.localhost_http_get(OTHER_PROJECT_PORT, "/private")

        assert "not allocated to this run's deployment" in str(exc.value)

    async def test_a_symlink_out_of_the_deployment_is_refused_by_the_target(self):
        """Containment is decided where the symlink is, from what it resolves to."""
        conn = FakeConn()

        async def run(command, *, check=False, timeout=None):
            conn.commands.append(command)
            if command.startswith("sh -c"):
                return SimpleNamespace(
                    exit_status=4,
                    stdout="",
                    stderr="outside:/opt/services/other-project/infra/.env\n",
                )
            return SimpleNamespace(exit_status=0, stdout="", stderr="")

        conn.run = run
        session = _session(conn)

        with pytest.raises(QATargetError) as exc:
            await session.read_file("evidence")

        assert "resolves outside this run's deployment" in str(exc.value)

    async def test_the_read_resolves_on_the_target_against_the_physical_root(self):
        conn = FakeConn()
        session = _session(conn)

        await session.read_file("infra/compose.yml")

        [command] = [c for c in conn.commands if c.startswith("sh -c")]
        assert "readlink -f" in command
        assert shlex.quote(PHYSICAL_ROOT) in command
        assert "infra/compose.yml" in command

    async def test_tools_carry_the_refusal_back_to_the_agent(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=_session(), workspace=workspace)
            }
            container = await tools["container_logs"].ainvoke(
                {"container": OTHER_PROJECT_CONTAINER}
            )
            port = await tools["localhost_http_get"].ainvoke(
                {"port": OTHER_PROJECT_PORT, "path": "/private"}
            )

        assert "is not a container of this run's deployment" in container["error"]
        assert "not allocated to this run's deployment" in port["error"]

    async def test_the_telegram_tool_addresses_the_bot_in_the_capability_set(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with_bot = QACapabilities(
            deployed_url=TARGET.deployed_url,
            physical_root=PHYSICAL_ROOT,
            containers=frozenset(OWN_CONTAINERS),
            loopback_ports=frozenset({8000}),
            bot_username="weather_bot",
        )
        sent: list[str] = []

        async def probe(script, *, env, timeout):
            sent.append(script)
            return SimpleNamespace(exit_status=0, stdout='telegram_replies:["hi"]\n', stderr="")

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool
                for tool in build_qa_tools(
                    session=_session(capabilities=with_bot),
                    workspace=workspace,
                    telethon_env={"TELETHON_SESSION": "s"},
                    probe_runner=probe,
                )
            }
            answer = await tools["telegram_probe"].ainvoke({"message": "/start"})

        assert answer["replies"] == ["hi"]
        assert "@weather_bot" in sent[0]
        assert "@weather_bot" in tools["telegram_probe"].description

    async def test_a_run_without_a_bot_has_no_telegram_tool(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with qa_workspace(root=str(tmp_path)) as workspace:
            names = {tool.name for tool in build_qa_tools(session=_session(), workspace=workspace)}

        assert "telegram_probe" not in names

    async def test_tool_descriptions_name_the_capability_that_bounds_them(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=_session(), workspace=workspace)
            }

        assert PHYSICAL_ROOT in tools["remote_read"].description
        assert "8000" in tools["localhost_http_get"].description
        assert OWN_CONTAINERS[0] in tools["container_logs"].description
        assert OTHER_PROJECT_CONTAINER not in tools["remote_exec"].description


class TestTheAgentHasNoShellAndNoHostView:
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
        ],
    )
    async def test_write_capable_commands_are_refused(self, argv):
        with pytest.raises(QATargetError):
            await _session().exec(argv)

    @pytest.mark.parametrize(
        "argv",
        [
            # Each of these describes the host rather than the deployment, so no
            # element of the capability set can bound it.
            ["docker", "ps", "-a"],
            ["docker", "images"],
            ["docker", "stats", "--no-stream"],
            ["docker", "version"],
            ["df", "-h"],
            ["uptime"],
            ["journalctl", "-u", "docker"],
        ],
    )
    async def test_host_wide_commands_are_refused(self, argv):
        with pytest.raises(QATargetError):
            await _session().exec(argv)

    async def test_a_container_scoped_read_is_allowed(self):
        conn = FakeConn()

        await _session(conn).exec(["docker", "top", OWN_CONTAINERS[0]])

        assert conn.commands == [f"docker top {OWN_CONTAINERS[0]}"]

    async def test_arguments_cannot_smuggle_a_shell(self):
        session = _session()

        with pytest.raises(QATargetError):
            await session.exec(["docker", "top", "weather-bot-backend-1; rm -rf /"])

    async def test_the_localhost_probe_can_only_get(self):
        conn = FakeConn()

        await _session(conn).localhost_http_get(8000, "/health")

        [command] = conn.commands
        assert "--get" in command
        assert "http://127.0.0.1:8000/health" in command
        assert "-X" not in command

    @pytest.mark.parametrize("path", ["health", "/hea lth"])
    async def test_the_localhost_probe_refuses_a_non_path(self, path):
        with pytest.raises(QATargetError):
            await _session().localhost_http_get(8000, path)

    @pytest.mark.parametrize("path", [".env", "infra/.env.prod", "keys/deploy.pem"])
    async def test_deployment_credentials_are_refused_before_the_read(self, path):
        conn = FakeConn()

        with pytest.raises(QATargetError):
            await _session(conn).read_file(path)

        assert conn.commands == []


class TestGrantIsDurableAndDestroyed:
    async def test_the_record_is_written_before_the_install(self, central_run):
        async def behaviour(graph):
            return PASSING_JSON

        _, _, _, journal = await central_run(behaviour=behaviour)

        assert journal.states[0] is QASshGrantState.ISSUING
        assert journal.states[1] is QASshGrantState.OPEN
        assert journal.states[-1] is QASshGrantState.RELEASED

    async def test_an_unconfirmed_install_leaves_an_open_record_and_a_reported_residue(
        self, tmp_path
    ):
        """The append may have landed; nothing here may conclude that it did not."""
        journal = RecordingJournal()

        async def behaviour(graph):
            return PASSING_JSON

        with (
            patch(
                "src.consumers._qa_target._connect",
                AsyncMock(side_effect=OSError("connection reset")),
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
                grant_journal=journal,
            )

        assert journal.states == [QASshGrantState.ISSUING]
        assert journal.grants[0].held is True
        # The early return used to report only "server unavailable"; the access
        # that may be standing has to reach the run's result.
        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert result.state_changes[0]["cleanup"]["succeeded"] is False

    async def test_a_failed_run_still_revokes_and_removes(self, central_run):
        async def behaviour(graph):
            raise RuntimeError("the agent died mid-run")

        result, conn, _, journal = await central_run(behaviour=behaviour)

        assert result.passed is False
        assert conn.authorized_keys == []
        assert journal.states[-1] is QASshGrantState.RELEASED
        assert result.blocker.category is QABlockerCategory.UNKNOWN

    async def test_a_cancelled_run_still_revokes(self):
        conn = FakeConn()
        outcome = QAGrantOutcome(marker=new_grant_marker())
        journal = RecordingJournal()

        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
            patch("src.consumers._qa_target._import", lambda key: key),
        ):
            with pytest.raises(RuntimeError):
                async with qa_target_grant(
                    target=TARGET,
                    fleet_ssh_key="fleet-key",
                    outcome=outcome,
                    journal=journal,
                ):
                    assert conn.authorized_keys
                    raise RuntimeError("cancelled")

        assert outcome.revoked is True
        assert conn.authorized_keys == []
        assert journal.states[-1] is QASshGrantState.RELEASED

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

        result, _, _, journal = await central_run(behaviour=behaviour, conn=StubbornConn())

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.QA_CLEANUP_FAILED
        assert result.state_changes[0]["cleanup"]["succeeded"] is False
        assert journal.states[-1] is QASshGrantState.OPEN
        assert journal.grants[-1].revoke_attempts == 1

    async def test_a_target_that_refuses_the_key_install_is_a_blocker(self):
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
                    target=TARGET,
                    fleet_ssh_key="fleet-key",
                    outcome=outcome,
                    journal=RecordingJournal(),
                ):
                    pass

        assert "Permission denied" in str(exc.value)


class TestWriteGuard:
    async def test_a_write_in_the_runner_trace_blocks_the_run(self, central_run):
        """The runner owns the trace, so a write it can see fails the run closed."""

        async def behaviour(graph):
            return (
                '{"pass": true, "checks": [{"name": "signup", "pass": true, '
                '"detail": "POST http://1.2.3.4:8000/users returned 201"}], "summary": "OK"}'
            )

        result, _, _, _ = await central_run(behaviour=behaviour)

        assert result.passed is False
        assert result.summary == "QA attempted a forbidden application API write"
        assert result.state_changes[0]["resource"] == "POST http://1.2.3.4:8000/users"

    async def test_the_trace_is_written_by_the_runner_not_the_agent(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with qa_workspace(root=str(tmp_path)) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=_session(), workspace=workspace)
            }
            await tools["remote_exec"].ainvoke({"command": ["docker", "top", OWN_CONTAINERS[0]]})
            trace = workspace.trace_path.read_text()

        assert '"tool": "remote_exec"' in trace
        assert "docker top" in trace
