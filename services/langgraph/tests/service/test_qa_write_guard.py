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
import inspect
import os
from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

os.environ.setdefault("API_BASE_URL", "http://localhost:8001")
os.environ.setdefault("INTERNAL_API_KEY", "test-key")

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import QABlockerCategory, QARunResult
from shared.contracts.queues.qa import QAServerInfo
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src.agents.qa.tools import build_qa_callables
from src.clients.qa_worker import QAExecutorRun
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import (
    QACapabilities,
    QAGrantError,
    QATarget,
    QATargetError,
    QATargetSession,
    _install_grant,
    revoke_grant,
)
from src.consumers._qa_workspace import qa_workspace
from src.consumers.qa import process_qa_job

# The run's only executor is the assigned subscription agent, addressing the
# runtime over loopback because in a test the "container" is this process.
_RUNTIME = QARuntimeConfig(executor_agent_type=AgentType.CLAUDE, capability_host="127.0.0.1")

OWNERSHIP = WorkerOwnership(project_id="proj-app", run_id="qa-run-1", attempt_id="attempt-qa-run-1")
ALLOWED_PORT = 8000
NEIGHBOUR_PORT = 9000
OWN_CONTAINER = "app-backend-1"
NEIGHBOUR_CONTAINER = "other-project-web-1"
RUNNING_STATE = (
    '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0,'
    '"Health":{"Status":"healthy"}}'
)


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
            ssh_user="root",
            qa_ssh_user="qa-observer",
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
            calls = build_qa_callables(session=session, workspace=workspace)
            refusal = await calls["remote_exec"](argv)
            # The read the agent is entitled to still works, and really reaches
            # the application — so an empty method list would prove nothing.
            await calls["http_get"]("/health")

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
            calls = build_qa_callables(session=session, workspace=workspace)
            assert "method" not in inspect.signature(calls["http_get"]).parameters
            answer = await calls["http_get"]("/users")

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


def _claimed_write_verdict(deployed_url: str) -> str:
    return (
        '{"pass": true, "checks": [{"name": "signup", "pass": true, "detail": '
        f'"POST {deployed_url}/users returned 201"}}], "summary": "OK"}}'
    )


class _FakeTargetConn:
    """Answers the grant and capability commands so the run reaches the agent."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        # The revoke script's last line is the count of the run's lines still in
        # the file; a target that answers nothing is residue, not a clean revoke.
        if "grep -c -F" in command:
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        if command.startswith("readlink -f --"):
            return SimpleNamespace(exit_status=0, stdout="/opt/services/app\n", stderr="")
        if "qa-docker ps" in command:
            return SimpleNamespace(exit_status=0, stdout=f"{OWN_CONTAINER}\n", stderr="")
        # The deterministic container-state probe the runner runs before the
        # executor. This deployment is up; what is under test here is the write
        # guard over what the executor then does.
        if "qa-docker inspect" in command:
            return SimpleNamespace(exit_status=0, stdout=RUNNING_STATE, stderr="")
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


class _ProvisioningJournal:
    """The provisioning journal. This target has its account, so nothing is written."""

    def __init__(self) -> None:
        self.entries = []

    async def missing_identity(self, *, reason, detail) -> None:
        self.entries.append((reason, detail))


def _writing_executor(deployed_url: str):
    """A central executor that claims a write — the fail-closed case.

    It reaches the run the way the real container does: over HTTP, holding
    nothing but the endpoint URL and this run's token.
    """

    async def run(
        *,
        agent_type,
        ownership,
        capability_url,
        capability_token,
        instructions,
        prompt,
        verdict_received,
        calls_served,
        timeout,
    ):
        async with aiohttp.ClientSession() as session:
            await session.post(
                capability_url,
                json={
                    "tool": "submit_qa_result",
                    "args": {"result": _claimed_write_verdict(deployed_url)},
                },
                headers={"Authorization": f"Bearer {capability_token}"},
            )
        return QAExecutorRun(
            verdict_submitted=verdict_received.is_set(),
            calls_served=calls_served(),
            detail="test executor",
        )

    return run


@pytest.mark.asyncio
async def test_a_claimed_write_blocks_the_run_with_a_residual_trace(tmp_path):
    """Evidence of a write fails the run closed, even when the agent says pass."""
    conn = _FakeTargetConn()
    with (
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "runs")),
        patch("src.consumers._qa_runner.run_qa_executor", _writing_executor("http://app.example")),
    ):
        result = await run_qa_centrally(
            target=QATarget(
                server_ip="1.2.3.4",
                ssh_user="root",
                qa_ssh_user="qa-observer",
                server_handle="vps-1",
                project_name="app",
                deployed_url="http://app.example",
                allocated_ports=frozenset({ALLOWED_PORT}),
            ),
            ownership=OWNERSHIP,
            fleet_ssh_key="fleet-key",
            acceptance_criteria="- read-only check",
            runtime=_RUNTIME,
            grant_journal=_Journal(),
            provisioning_journal=_ProvisioningJournal(),
            established_facts=[],
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
    api_client.get_run = AsyncMock(
        return_value=SimpleNamespace(
            run_metadata=ExecutorDecision(
                attempt_kind=RunType.QA,
                agent_type=AgentType.CLAUDE,
                source=ExecutorDecisionSource.QA_API_SETTING,
                policy_version="v1",
                reason="QA executor selected by API QA_EXECUTOR_AGENT_TYPE.",
            ).as_run_metadata()
        )
    )

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
                ssh_user="root",
                qa_ssh_user="qa-observer",
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
        patch("src.consumers._qa_runner.run_qa_executor", _writing_executor("http://app.example")),
    ):
        get_settings.return_value = SimpleNamespace(
            qa_executor_agent_type=AgentType.CLAUDE,
            qa_capability_host="127.0.0.1",
        )
        await process_qa_job(
            {
                "story_id": "story-1",
                "project_id": "project-1",
                "initiating_run_id": "live-1",
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


class _AccountShellConn(_LocalShellConn):
    """A target where one account exists, with a home under `tmp_path`.

    The install and revoke scripts find the account's home with `getent passwd`,
    which is the only part of them a test cannot have for real: everything else —
    the lock, the append, the filter, the readback — runs against actual files.
    So `getent` is the one thing stubbed, and it is stubbed on PATH rather than
    in the script, which stays exactly the text the runtime sends.
    """

    def __init__(self, account: str, home: Path) -> None:
        super().__init__()
        self._bin = home.parent / "stub-bin"
        self._bin.mkdir(exist_ok=True)
        getent = self._bin / "getent"
        getent.write_text(
            "#!/bin/sh\n"
            f'[ "$2" = "{account}" ] || exit 2\n'
            f'printf "%s\\n" "{account}:x:1001:1001::{home}:/bin/bash"\n'
        )
        getent.chmod(0o755)

    async def run(self, command: str, *, check: bool = False, timeout: float | None = None):
        self.commands.append(command)
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{self._bin}:{os.environ['PATH']}"},
        )
        return SimpleNamespace(stdout=proc.stdout, stderr=proc.stderr, exit_status=proc.returncode)


class TestTheGrantScriptsAgainstARealAuthorizedKeysFile:
    """The scripts touch one account's file, so a real one is used.

    A fake connection can only confirm the shell text this code sends. What
    matters here is what that text does to a file: that it appends to the QA
    account's `authorized_keys` and creates nothing, that it removes exactly the
    run's own line, and what it does when the filter behind the removal comes
    back empty. That last case is why the provisioning role opens the file with a
    comment line — an empty result means the filter failed, and copying it over
    the file would leave a file the next run cannot use.
    """

    ACCOUNT = "qa-observer"
    SENTINEL = "# codegen-qa: run keys are added and removed here\n"

    def _account(self, tmp_path: Path, *, keys: str | None) -> tuple[Path, _AccountShellConn]:
        home = tmp_path / "home"
        (home / ".ssh").mkdir(parents=True)
        authorized = home / ".ssh" / "authorized_keys"
        if keys is not None:
            authorized.write_text(keys)
        return authorized, _AccountShellConn(self.ACCOUNT, home)

    def _connected(self, conn: _AccountShellConn):
        from src.consumers import _qa_target

        return patch.multiple(
            _qa_target, _connect=AsyncMock(return_value=conn), _import=lambda key: key
        )

    async def _revoke(self, conn: _AccountShellConn, marker: str) -> str | None:
        with self._connected(conn):
            return await revoke_grant(
                server_ip="127.0.0.1",
                ssh_user="root",
                qa_ssh_user=self.ACCOUNT,
                fleet_key="k",
                marker=marker,
            )

    @pytest.mark.asyncio
    async def test_the_key_is_appended_to_the_qa_accounts_own_file(self, tmp_path):
        keys, conn = self._account(tmp_path, keys=self.SENTINEL)

        with self._connected(conn):
            await _install_grant(
                QATarget(
                    server_ip="127.0.0.1",
                    ssh_user="root",
                    qa_ssh_user=self.ACCOUNT,
                    server_handle="vps-1",
                    project_name="app",
                    deployed_url="http://app.example",
                ),
                "fleet-key",
                "restrict ssh-ed25519 RUNKEY marker-1",
            )

        assert keys.read_text() == f"{self.SENTINEL}restrict ssh-ed25519 RUNKEY marker-1\n"

    @pytest.mark.asyncio
    async def test_an_account_without_the_file_is_refused_not_opened(self, tmp_path):
        """Provisioning opens this file. A runtime that opened it would be root."""
        keys, conn = self._account(tmp_path, keys=None)

        with self._connected(conn), pytest.raises(QAGrantError):
            await _install_grant(
                QATarget(
                    server_ip="127.0.0.1",
                    ssh_user="root",
                    qa_ssh_user=self.ACCOUNT,
                    server_handle="vps-1",
                    project_name="app",
                    deployed_url="http://app.example",
                ),
                "fleet-key",
                "restrict ssh-ed25519 RUNKEY marker-1",
            )

        assert not keys.exists()

    @pytest.mark.asyncio
    async def test_only_the_runs_own_line_is_removed(self, tmp_path):
        keys, conn = self._account(tmp_path, keys=f"{self.SENTINEL}ssh-ed25519 RUNKEY marker-1\n")

        residual = await self._revoke(conn, "marker-1")

        assert residual is None
        assert keys.read_text() == self.SENTINEL

    @pytest.mark.asyncio
    async def test_a_marker_that_was_never_installed_reads_back_clean(self, tmp_path):
        """The ambiguous case the sweep retries: nothing was installed after all."""
        keys, conn = self._account(tmp_path, keys=self.SENTINEL)

        residual = await self._revoke(conn, "never-installed")

        assert residual is None
        assert keys.read_text() == self.SENTINEL

    @pytest.mark.asyncio
    async def test_an_empty_filter_result_is_never_copied_over_the_file(self, tmp_path):
        """A file of nothing but our own line means the filter failed, not that we own it."""
        keys, conn = self._account(tmp_path, keys="ssh-ed25519 RUNKEY marker-1\n")

        residual = await self._revoke(conn, "marker-1")

        # The file is left exactly as it was, and the readback says so — which is
        # what puts the grant in front of the sweep instead of closing it.
        assert keys.read_text() == "ssh-ed25519 RUNKEY marker-1\n"
        assert residual is not None
        assert "marker-1" in residual

    @pytest.mark.asyncio
    async def test_an_account_that_is_gone_holds_no_key(self, tmp_path):
        _, conn = self._account(tmp_path, keys=self.SENTINEL)

        assert await self._revoke(conn, "marker-1") is None
        with (
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        ):
            gone = await revoke_grant(
                server_ip="127.0.0.1",
                ssh_user="root",
                qa_ssh_user="no-such-account",
                fleet_key="k",
                marker="marker-1",
            )

        assert gone is None
