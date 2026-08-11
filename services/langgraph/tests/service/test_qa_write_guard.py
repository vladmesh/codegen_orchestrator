"""QA-run boundary tests against a real filesystem and a real HTTP server.

Two guarantees are checked here rather than in the unit tests, because both are
about what a real target does rather than what this code intends.

The read-only guarantee: the guard used to be a Claude PreToolUse hook filtering
a Bash command line. There is no Bash and no on-target agent any more, so it is
enforced one layer lower — the tools cannot express a write. A real HTTP server
stands in for the deployed application, and it must never see a method other
than GET.

The containment guarantee: a lexical path check is satisfied by a symlink that
points anywhere. So the read is driven through a real shell, on a real directory
tree, with a real symlink into a neighbouring project — and the neighbour's file
has to stay unread.

The last part is fail-closed: if a write does turn up in the evidence the runner
owns, the run is quarantined with a residual trace rather than reported as a QA
result.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import subprocess
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.run_result import QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAServerInfo
from src.agents.qa.tools import build_qa_tools
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import (
    QACapabilities,
    QATarget,
    QATargetError,
    QATargetSession,
    revoke_grant,
)
from src.consumers._qa_workspace import qa_workspace
from src.consumers.qa import process_qa_job

ALLOWED_PORT = 8000
NEIGHBOUR_PORT = 9000
OWN_CONTAINER = "app-backend-1"
NEIGHBOUR_CONTAINER = "other-project-web-1"


class _LocalShellConn:
    """A "target" that really runs what the typed tools ask it to run."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            check=False,
        )
        return SimpleNamespace(stdout=proc.stdout, stderr=proc.stderr, exit_status=proc.returncode)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _RecordingApplication:
    """The deployed application, recording every method it is asked for."""

    def __init__(self) -> None:
        self.methods: list[str] = []
        application = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                application.methods.append(self.command)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            do_GET = _record
            do_POST = _record
            do_PUT = _record
            do_PATCH = _record
            do_DELETE = _record

            def log_message(self, format, *args):  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        return False

    @property
    def port(self) -> int:
        return self._server.server_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _deployment_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A deployment directory and a neighbouring project's secret beside it."""
    deployment = tmp_path / "services" / "app"
    (deployment / "infra").mkdir(parents=True)
    (deployment / "infra" / "compose.yml").write_text("services: {}\n")

    neighbour = tmp_path / "services" / "other-project"
    neighbour.mkdir(parents=True)
    neighbour_secret = neighbour / "database.conf"
    neighbour_secret.write_text("PASSWORD=neighbour-secret\n")

    # Valid git and filesystem content: a symlink in the deployed tree that
    # leads out of it.
    (deployment / "evidence").symlink_to(neighbour_secret)
    return deployment, neighbour_secret


def _session(deployment: Path, deployed_url: str, conn: _LocalShellConn) -> QATargetSession:
    return QATargetSession(
        QATarget(
            server_ip="127.0.0.1",
            ssh_user="qa",
            server_handle="vps-1",
            project_name="app",
            deployed_url=deployed_url,
            allocated_ports=frozenset({ALLOWED_PORT}),
        ),
        conn,
        QACapabilities(
            deployed_url=deployed_url,
            physical_root=str(deployment.resolve()),
            containers=frozenset({OWN_CONTAINER}),
            loopback_ports=frozenset({ALLOWED_PORT}),
        ),
    )


@pytest.fixture(autouse=True)
async def _clean_redis():
    """This runner-boundary test does not use Redis."""
    yield


# Every shape the old Bash guard had to filter. None of them is expressible now:
# there is no tool that takes a method, and no tool that takes a shell.
WRITE_ATTEMPTS = (
    ["curl", "-X", "POST", "{url}/users"],
    ["curl", "{url}/users", "-X", "PATCH", "-d", "{}"],
    ["python3", "-c", "import requests; requests.post('{url}/users')"],
    ["sh", "-c", "curl -d '{}' {url}/users"],
    ["wget", "--method=DELETE", "{url}/users"],
    ["docker", "exec", OWN_CONTAINER, "sh", "-c", "curl -XPOST {url}/users"],
)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", WRITE_ATTEMPTS)
async def test_no_tool_can_send_a_write_to_the_application(tmp_path, attempt):
    deployment, _ = _deployment_tree(tmp_path)
    conn = _LocalShellConn()
    with _RecordingApplication() as application:
        session = _session(deployment, application.url, conn)
        argv = [part.replace("{url}", application.url) for part in attempt]

        with qa_workspace(root=str(tmp_path / "runs")) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=session, workspace=workspace)
            }
            refusal = await tools["remote_exec"].ainvoke({"command": argv})
            # The read the agent is entitled to still works, and really reaches
            # the application — so an empty method list would prove nothing.
            await tools["http_get"].ainvoke({"path": "/health"})

        assert "error" in refusal
        assert application.methods == ["GET"]
    # Nothing was even sent to the target: the refusal happens before the wire.
    assert conn.commands == []


@pytest.mark.asyncio
async def test_the_public_probe_is_get_only(tmp_path):
    deployment, _ = _deployment_tree(tmp_path)
    with _RecordingApplication() as application:
        session = _session(deployment, application.url, _LocalShellConn())
        with qa_workspace(root=str(tmp_path / "runs")) as workspace:
            tools = {
                tool.name: tool for tool in build_qa_tools(session=session, workspace=workspace)
            }
            assert "method" not in tools["http_get"].args
            answer = await tools["http_get"].ainvoke({"path": "/users"})

        assert answer["status"] == 200
        assert application.methods == ["GET"]


class TestPhysicalContainmentAgainstARealSymlink:
    """A neighbouring project on the same host, reached by a real symlink."""

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_deployment_does_not_read_the_neighbour(self, tmp_path):
        deployment, neighbour_secret = _deployment_tree(tmp_path)
        conn = _LocalShellConn()
        session = _session(deployment, "http://app.example", conn)

        with pytest.raises(QATargetError) as exc:
            await session.read_file("evidence")

        assert "resolves outside this run's deployment" in str(exc.value)
        # The refusal is decided from what the path resolved to on the target,
        # and the neighbour's content never came back.
        assert neighbour_secret.read_text() not in " ".join(conn.commands)

    @pytest.mark.asyncio
    async def test_an_absolute_path_into_the_neighbour_is_refused(self, tmp_path):
        deployment, neighbour_secret = _deployment_tree(tmp_path)
        session = _session(deployment, "http://app.example", _LocalShellConn())

        with pytest.raises(QATargetError):
            await session.read_file(str(neighbour_secret))

    @pytest.mark.asyncio
    async def test_a_file_inside_the_deployment_is_read(self, tmp_path):
        deployment, _ = _deployment_tree(tmp_path)
        session = _session(deployment, "http://app.example", _LocalShellConn())

        result = await session.read_file("infra/compose.yml")

        assert result.exit_status == 0
        assert result.stdout == "services: {}\n"

    @pytest.mark.asyncio
    async def test_traversal_out_of_the_deployment_is_refused(self, tmp_path):
        deployment, _ = _deployment_tree(tmp_path)
        session = _session(deployment, "http://app.example", _LocalShellConn())

        with pytest.raises(QATargetError):
            await session.read_file("../other-project/database.conf")


class TestANeighbourOnTheSameHost:
    @pytest.mark.asyncio
    async def test_a_port_the_deployment_does_not_own_is_never_contacted(self, tmp_path):
        deployment, _ = _deployment_tree(tmp_path)
        conn = _LocalShellConn()
        with _RecordingApplication() as neighbour:
            session = _session(deployment, "http://app.example", conn)

            with pytest.raises(QATargetError):
                await session.localhost_http_get(neighbour.port, "/private")

            assert neighbour.methods == []
        assert conn.commands == []

    @pytest.mark.asyncio
    async def test_a_container_the_deployment_does_not_own_is_never_named(self, tmp_path):
        deployment, _ = _deployment_tree(tmp_path)
        conn = _LocalShellConn()
        session = _session(deployment, "http://app.example", conn)

        with pytest.raises(QATargetError):
            await session.container_logs(NEIGHBOUR_CONTAINER)
        with pytest.raises(QATargetError):
            await session.exec(["docker", "inspect", NEIGHBOUR_CONTAINER])

        assert conn.commands == []


class _WritingAgent:
    """An agent that claims a write in its own result — the fail-closed case."""

    def __init__(self, deployed_url: str) -> None:
        self.deployed_url = deployed_url

    async def ainvoke(self, state, config=None):
        return {
            "messages": [
                SimpleNamespace(
                    content=(
                        '{"pass": true, "checks": [{"name": "signup", "pass": true, "detail": '
                        f'"POST {self.deployed_url}/users returned 201"}}], "summary": "OK"}}'
                    )
                )
            ]
        }


class _FakeTargetConn:
    """Answers the grant and capability commands so the run reaches the agent."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        if command.startswith("grep -c -F"):
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        if command.startswith("readlink -f --"):
            return SimpleNamespace(exit_status=0, stdout="/opt/services/app\n", stderr="")
        if command.startswith("docker ps"):
            return SimpleNamespace(exit_status=0, stdout=f"{OWN_CONTAINER}\n", stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Journal:
    def __init__(self) -> None:
        self.states = []

    async def write(self, grant) -> None:
        self.states.append(grant.state)


def _writing_graph(deployed_url: str):
    def create(*, model, base_url, api_key, tools, prompt):
        return _WritingAgent(deployed_url)

    return create


@pytest.mark.asyncio
async def test_a_claimed_write_blocks_the_run_with_a_residual_trace(tmp_path):
    """Evidence of a write fails the run closed, even when the agent says pass."""
    conn = _FakeTargetConn()
    with (
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "runs")),
        patch("src.consumers._qa_runner.create_qa_graph", _writing_graph("http://app.example")),
    ):
        result = await run_qa_centrally(
            target=QATarget(
                server_ip="1.2.3.4",
                ssh_user="qa",
                server_handle="vps-1",
                project_name="app",
                deployed_url="http://app.example",
                allocated_ports=frozenset({ALLOWED_PORT}),
            ),
            fleet_ssh_key="fleet-key",
            acceptance_criteria="- read-only check",
            runtime=QARuntimeConfig(model="m", base_url="u", api_key="k"),
            grant_journal=_Journal(),
        )

    assert result.passed is False
    assert result.blocker is not None
    assert result.blocker.category is QABlockerCategory.UNKNOWN
    assert result.state_changes[0]["resource"] == "POST http://app.example/users"
    assert result.state_changes[0]["cleanup"]["succeeded"] is False


@pytest.mark.asyncio
async def test_qa_consumer_quarantines_a_write_trace(tmp_path):
    """The consumer persists the runner-created trace as a quarantine blocker."""
    conn = _FakeTargetConn()
    redis = AsyncMock()
    redis.redis.set = AsyncMock(return_value=True)
    redis.redis.delete = AsyncMock()
    api_client = AsyncMock()
    api_client.patch = AsyncMock()
    api_client.get_project = AsyncMock(return_value=SimpleNamespace(slug="app", config={}))

    with (
        patch("src.consumers.qa.api_client", api_client),
        patch(
            "src.consumers.qa.check_deployed_url_reachable",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "src.consumers.qa._resolve_server_info",
            new_callable=AsyncMock,
            return_value=QAServerInfo(
                server_ip="1.2.3.4",
                ssh_user="qa",
                ssh_key="fake",
                project_name="app",
                server_handle="vps-1",
                allocated_ports=frozenset({ALLOWED_PORT}),
            ),
        ),
        patch("src.consumers.qa.get_settings") as get_settings,
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "runs")),
        patch("src.consumers._qa_runner.create_qa_graph", _writing_graph("http://app.example")),
    ):
        get_settings.return_value = SimpleNamespace(
            qa_llm_model="m", qa_llm_base_url="u", qa_llm_api_key="k"
        )
        await process_qa_job(
            {
                "story_id": "story-1",
                "project_id": "project-1",
                "telegram_chat_id": "1",
                "deployed_url": "http://app.example",
                "application_id": 1,
                "acceptance_criteria": "- bot replies to /start",
                "run_id": "qa-run-1",
                "qa_attempt": 0,
            },
            redis,
        )

    persisted = api_client.patch.await_args_list[-1].kwargs["json"]["result"]
    result = QARunResult.model_validate(persisted)
    assert result.qa_outcome.value == "blocked"
    assert result.blocker is not None
    assert result.state_changes[0].resource == "POST http://app.example/users"
    assert result.state_changes[0].cleanup.succeeded is False


class TestRevokeRewritesARealAuthorizedKeysFile:
    """The revoke edits the file that authorizes the fleet, so a real one is used.

    A fake connection can only confirm the shell text this code sends. What
    matters here is what that text does to a file, and specifically what it does
    when the filter behind it comes back empty: copying that result over
    `authorized_keys` would take the fleet's own line with it and lock the
    orchestrator out of the target permanently.
    """

    @staticmethod
    def _revoke(tmp_path: Path, keys: Path):
        from src.consumers import _qa_target

        return patch.multiple(
            _qa_target,
            AUTHORIZED_KEYS=shlex.quote(str(keys)),
            GRANT_LOCK=shlex.quote(str(tmp_path / "qa.lock")),
            _connect=AsyncMock(return_value=_LocalShellConn()),
            _import=lambda key: key,
        )

    @pytest.mark.asyncio
    async def test_only_the_runs_own_line_is_removed(self, tmp_path):
        keys = tmp_path / "authorized_keys"
        keys.write_text("ssh-ed25519 FLEETKEY orchestrator\nssh-ed25519 RUNKEY marker-1\n")

        with self._revoke(tmp_path, keys):
            residual = await revoke_grant(
                server_ip="127.0.0.1", ssh_user="qa", fleet_key="k", marker="marker-1"
            )

        assert residual is None
        assert keys.read_text() == "ssh-ed25519 FLEETKEY orchestrator\n"

    @pytest.mark.asyncio
    async def test_a_marker_that_was_never_installed_reads_back_clean(self, tmp_path):
        """The ambiguous case the sweep retries: nothing was installed after all."""
        keys = tmp_path / "authorized_keys"
        keys.write_text("ssh-ed25519 FLEETKEY orchestrator\n")

        with self._revoke(tmp_path, keys):
            residual = await revoke_grant(
                server_ip="127.0.0.1", ssh_user="qa", fleet_key="k", marker="never-installed"
            )

        assert residual is None
        assert keys.read_text() == "ssh-ed25519 FLEETKEY orchestrator\n"

    @pytest.mark.asyncio
    async def test_an_empty_filter_result_is_never_copied_over_the_file(self, tmp_path):
        """A file of nothing but our own line means the filter failed, not that we own it."""
        keys = tmp_path / "authorized_keys"
        keys.write_text("ssh-ed25519 RUNKEY marker-1\n")

        with self._revoke(tmp_path, keys):
            residual = await revoke_grant(
                server_ip="127.0.0.1", ssh_user="qa", fleet_key="k", marker="marker-1"
            )

        # The file is left exactly as it was, and the readback says so — which is
        # what puts the grant in front of the sweep instead of closing it.
        assert keys.read_text() == "ssh-ed25519 RUNKEY marker-1\n"
        assert residual is not None
        assert "marker-1" in residual
