"""The central QA runtime: a clean target, one deployment, and nothing left behind.

These tests drive `run_qa_centrally` with a fake SSH layer that answers only the
commands the runner actually sends. That is the point: the target in these tests
has no Claude CLI, no LLM credentials and no Telethon session, and a run that
needed any of them would have to ask for it here, where every command is
recorded.

The executor is the assigned subscription coding agent, and it is stood in for
the way the real one behaves: a separate process that holds nothing, reaching
the run only by posting named calls to the capability endpoint over real HTTP.
So the boundary these tests check is the one the container actually meets, not a
Python closure it would never be given.

The other half is the capability set. A run's boundary is one object resolved
from deployment data — physical root, containers, loopback ports, public URL —
and every refusal below is a membership test against it. Where a second project
shares the host, that is what has to refuse.
"""

from __future__ import annotations

from dataclasses import replace
import re
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from shared.contracts.dto.qa_ssh_grant import QASshGrantState
from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.qa_identity import QAIdentityRejection
from src.clients.qa_worker import QAExecutorRun, QAExecutorUnavailable
from src.consumers._qa_runner import QARuntimeConfig, run_qa_centrally
from src.consumers._qa_target import (
    GRANT_MARKER_PREFIX,
    QA_DOCKER,
    QA_DOCKER_WRAPPER,
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
    revoke_grant,
)
from src.consumers._qa_workspace import qa_workspace

# Two accounts, and the difference between them is the point: `ssh_user` is what
# the fleet key opens (root on a row `server_sync` created) and is used only to
# put the run's key in and take it out; `qa_ssh_user` is the unprivileged account
# provisioning made, and is who the run is.
TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="root",
    qa_ssh_user="qa-observer",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000, 8001}),
)
# The line the qa_identity role opens `authorized_keys` with. It is never a key
# and never carries a run marker, which is what keeps "the filter kept nothing"
# a failure rather than a legitimately emptied file.
SENTINEL = "# codegen-qa: one-shot run keys are added and removed here"
# What the target reports back about this deployment. Another project's
# containers and ports exist on the same host and are absent from it.
OWN_CONTAINERS = ("weather-bot-backend-1", "weather-bot-db-1")
OTHER_PROJECT_CONTAINER = "other-project-web-1"
OTHER_PROJECT_PORT = 9000
PHYSICAL_ROOT = "/srv/deployments/weather-bot"
# What docker reports for a container of a deployment that is up. The runner
# reads this deterministically before any executor starts, so every fake target
# here has to be able to answer it.
RUNNING_STATE = (
    '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0,'
    '"Health":{"Status":"healthy"}}'
)

# Claude Code on the host's subscription session, addressing the runtime over
# loopback because in a test the "container" is this process. No API triplet:
# `NO_API_FALLBACK` is the production configuration these runs must work in.
RUNTIME = QARuntimeConfig(executor_agent_type=AgentType.CLAUDE, capability_host="127.0.0.1")
NO_API_FALLBACK = SimpleNamespace(qa_llm_model=None, qa_llm_base_url=None, qa_llm_api_key=None)
API_FALLBACK = SimpleNamespace(
    qa_llm_model="m",
    qa_llm_base_url="http://llm.invalid/v1",
    qa_llm_api_key="sk-qa-fallback-not-real",
)
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


class RecordingProvisioningJournal:
    """Stands in for the provisioning journal an administrator reads.

    Only one kind of fact is ever written here, and which failures reach it is
    the point: a host that lost the account provisioning made is a provisioning
    fact, while an unreachable target or a dead agent is this run's problem and
    has its own owner.
    """

    def __init__(self) -> None:
        self.entries: list[tuple[QAIdentityRejection, str]] = []

    async def missing_identity(self, *, reason: QAIdentityRejection, detail: str) -> None:
        self.entries.append((reason, detail))


class FakeConn:
    """A target that answers the runner's typed commands and records them all.

    It models the one file the runtime is allowed to touch — the QA account's
    `authorized_keys`, as provisioning left it — and the two scripts that touch
    it. `provisioned=False` is a host where that account or that file does not
    exist, which is what the runtime has to refuse rather than create.
    """

    def __init__(
        self,
        containers: tuple[str, ...] = OWN_CONTAINERS,
        *,
        provisioned: bool = True,
        container_states: dict[str, str] | None = None,
    ) -> None:
        self.commands: list[str] = []
        self.authorized_keys: list[str] | None = [SENTINEL] if provisioned else None
        self.installed: list[str] = []
        self.written_as: list[str] = []
        self.containers = containers
        # What `docker inspect --format {{json .State}}` answers per container.
        # Every container of the deployment is up unless a test says otherwise.
        self.container_states = container_states or dict.fromkeys(containers, RUNNING_STATE)
        self.closed = False

    @property
    def run_keys(self) -> list[str]:
        """The run keys standing in that file right now."""
        return [line for line in (self.authorized_keys or []) if GRANT_MARKER_PREFIX in line]

    @staticmethod
    def _script(command: str) -> tuple[str, list[str]] | None:
        """The script body and its arguments, if this command is one of ours."""
        tokens = shlex.split(command)
        if tokens[:2] != ["sh", "-c"]:
            return None
        # sh -c BODY _ ARG...  — the `_` is $0, never an argument.
        return tokens[2], tokens[4:]

    def _install(self, user: str, entry: str):
        self.written_as.append(user)
        if self.authorized_keys is None:
            return SimpleNamespace(
                exit_status=4, stdout="", stderr=f"no authorized_keys for {user}"
            )
        self.authorized_keys.append(entry)
        self.installed.append(entry)
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    def _revoke(self, user: str, marker: str):
        self.written_as.append(user)
        if self.authorized_keys is None:
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        kept = [line for line in self.authorized_keys if marker not in line]
        # The script copies the filter result back only when it kept something.
        if kept:
            self.authorized_keys = kept
        remaining = sum(1 for line in self.authorized_keys if marker in line)
        return SimpleNamespace(exit_status=0, stdout=f"{remaining}\n", stderr="")

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        script = self._script(command)
        if script:
            body, args = script
            if "printf" in body:
                return self._install(*args)
            if "grep -c -F" in body:
                return self._revoke(*args)
        if command.startswith("readlink -f --"):
            return SimpleNamespace(exit_status=0, stdout=f"{PHYSICAL_ROOT}\n", stderr="")
        if QA_DOCKER_WRAPPER in command and " ps " in command:
            return SimpleNamespace(
                exit_status=0, stdout="".join(f"{name}\n" for name in self.containers), stderr=""
            )
        if QA_DOCKER_WRAPPER in command and " inspect " in command:
            name = shlex.split(command)[-1]
            return SimpleNamespace(exit_status=0, stdout=self.container_states[name], stderr="")
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


class RemoteCall:
    """One named call, made the way the executor's container makes it."""

    def __init__(self, harness, name: str) -> None:
        self._harness = harness
        self.name = name

    async def ainvoke(self, args: dict):
        return await self._harness.call(self.name, **args)


class ExecutorHarness:
    """The executor as it really is: a process holding a URL and a token.

    It has no session, no key and no callable — it posts to the endpoint like
    the injected `qa` command does, so every refusal a test sees here is one the
    endpoint made, not one a Python closure made on the caller's behalf.
    """

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self.tools = _ToolLookup(self)

    async def call(self, name: str, **args):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._url,
                json={"tool": name, "args": args},
                headers={"Authorization": f"Bearer {self._token}"},
            ) as response:
                return await response.json()

    async def submit(self, raw: str):
        return await self.call("submit_qa_result", result=raw)


class _ToolLookup:
    def __init__(self, harness: ExecutorHarness) -> None:
        self._harness = harness

    def __getitem__(self, name: str) -> RemoteCall:
        return RemoteCall(self._harness, name)


def _executor_factory(behaviour, *, unavailable: QAExecutorUnavailable | None = None):
    """Stand in for `run_qa_executor`, driving the endpoint as a container would."""

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
        run.prompt = prompt
        run.instructions = instructions
        run.agent_type = agent_type
        run.ownership = ownership
        if unavailable is not None:
            raise unavailable
        harness = ExecutorHarness(capability_url, capability_token)
        raw = await behaviour(harness)
        if raw is not None:
            await harness.submit(raw)
        return QAExecutorRun(
            verdict_submitted=verdict_received.is_set(),
            calls_served=calls_served(),
            detail=f"{agent_type.value} executor (test)",
        )

    return run


def _session(conn=None, capabilities: QACapabilities = CAPABILITIES) -> QATargetSession:
    return QATargetSession(TARGET, conn or FakeConn(), capabilities)


@pytest.fixture
def central_run(tmp_path):
    """Run `run_qa_centrally` against a fake target with a scripted executor."""

    async def _run(
        *,
        behaviour,
        conn=None,
        target=TARGET,
        journal=None,
        provisioning=None,
        settings=NO_API_FALLBACK,
        unavailable=None,
        runtime=RUNTIME,
    ):
        connection = conn or FakeConn()
        factory = _executor_factory(behaviour, unavailable=unavailable)
        record = journal or RecordingJournal()
        provisioning_record = provisioning or RecordingProvisioningJournal()
        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=connection)),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.run_qa_executor", factory),
        ):
            result = await run_qa_centrally(
                target=target,
                ownership=WorkerOwnership(
                    project_id="proj-qa", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=runtime,
                grant_journal=record,
                provisioning_journal=provisioning_record,
                settings=settings,
                established_facts=[],
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

    async def test_no_credential_of_any_kind_is_sent_to_the_target(self, tmp_path):
        """AC6, from the target's side, with every credential this run has present.

        The runtime holds the fleet key, the QA Telegram session and — in this
        test — a configured API fallback as well. The target must see none of
        them, whichever executor runs and whatever the executor asks for. The
        assertion is over everything that crossed the SSH connection, which is
        the whole of what the target can observe.
        """
        conn = FakeConn()
        secrets = {
            "TELETHON_API_ID": "12345",
            "TELETHON_API_HASH": "hash-value-9f2",
            "TELETHON_SESSION": "1BQANOTEuMTA4LjU2LjE",
        }
        runtime = QARuntimeConfig(
            executor_agent_type=AgentType.CLAUDE,
            capability_host="127.0.0.1",
            telethon_env=secrets,
        )

        async def behaviour(harness):
            # A run that asks for everything it is allowed to ask for.
            await harness.tools["container_logs"].ainvoke({"container": OWN_CONTAINERS[0]})
            await harness.tools["container_inspect"].ainvoke({"container": OWN_CONTAINERS[0]})
            await harness.tools["remote_read"].ainvoke({"path": "infra/compose.yml"})
            await harness.tools["localhost_http_get"].ainvoke({"port": 8000, "path": "/health"})
            return PASSING_JSON

        with (
            patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.run_qa_executor", _executor_factory(behaviour)),
        ):
            result = await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    project_id="proj-qa", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
                ),
                fleet_ssh_key="-----BEGIN OPENSSH PRIVATE KEY-----\nfleet\n-----END-----",
                acceptance_criteria="- GET /health returns 200",
                runtime=runtime,
                grant_journal=RecordingJournal(),
                provisioning_journal=RecordingProvisioningJournal(),
                settings=API_FALLBACK,
                established_facts=[],
            )

        assert result.passed is True
        sent = "\n".join(conn.commands)
        for secret in (
            *secrets.values(),
            "BEGIN OPENSSH PRIVATE KEY",
            API_FALLBACK.qa_llm_api_key,
            "ANTHROPIC_API_KEY",
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
            ".credentials.json",
        ):
            assert secret not in sent, f"{secret!r} reached the target"

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
            patch("src.consumers._qa_runner.run_qa_executor", _executor_factory(behaviour)),
        ):
            await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    project_id="proj-qa", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
                grant_journal=RecordingJournal(),
                provisioning_journal=RecordingProvisioningJournal(),
                settings=NO_API_FALLBACK,
                established_facts=[],
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


class TestTheRunBorrowsTheAccountProvisioningMade:
    """The identity is provisioned; the runtime only borrows it for one run."""

    async def test_the_key_lands_in_the_qa_account_and_the_run_connects_as_it(self, tmp_path):
        connected: list[str] = []
        conn = FakeConn()

        async def connect(server_ip, ssh_user, key):
            connected.append(ssh_user)
            return conn

        async def behaviour(graph):
            return PASSING_JSON

        with (
            patch("src.consumers._qa_target._connect", connect),
            patch("src.consumers._qa_target._import", lambda key: key),
            patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
            patch("src.consumers._qa_runner.run_qa_executor", _executor_factory(behaviour)),
        ):
            result = await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    project_id="proj-qa", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
                grant_journal=RecordingJournal(),
                provisioning_journal=RecordingProvisioningJournal(),
                settings=NO_API_FALLBACK,
                established_facts=[],
            )

        assert result.passed is True
        # The administrative account installs and removes; the run itself is the
        # unprivileged one. Nothing here is ever performed as `ssh_user`.
        assert connected == ["root", "qa-observer", "root"]
        # And both writes went into the QA account's file, not the admin's.
        assert conn.written_as == ["qa-observer", "qa-observer"]

    async def test_a_target_without_the_account_is_refused_rather_than_provisioned(self):
        """A runtime that could open the account would be a runtime with root."""
        conn = FakeConn(provisioned=False)
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

        assert "qa-observer" in str(exc.value)
        assert conn.authorized_keys is None
        assert conn.installed == []

    async def test_a_target_that_lost_the_account_is_a_provisioning_fact(self, central_run):
        """The row promised an account; the host has none. That is drift, and it shows.

        A host provisioned before the account was deleted by hand, or whose home
        was cleaned up, carries a correct `qa_ssh_user` label and no account. The
        label check upstream cannot see that, and the install is where it comes
        out — as the same missing identity, one step later. Ending as a blocked
        run in this consumer's log would leave an administrator with nothing to
        look at, so it goes to the journal that names the server.
        """
        provisioning = RecordingProvisioningJournal()

        async def behaviour(graph):
            return PASSING_JSON

        result, conn, _, _ = await central_run(
            behaviour=behaviour,
            conn=FakeConn(provisioned=False),
            provisioning=provisioning,
        )

        assert result.passed is False
        # The run still carries whatever this exit leaves unresolved — an
        # install that raised is residue until something reads the target back,
        # and that rule is older than this one and stays.
        assert result.blocker is not None
        assert "records a QA account but" in result.summary
        [(reason, detail)] = provisioning.entries
        assert reason is QAIdentityRejection.ABSENT_ON_TARGET
        assert TARGET.server_handle in detail
        # And nothing was created to make the run possible: the runtime has no
        # power to give itself the account provisioning did not leave.
        assert conn.authorized_keys is None
        assert conn.installed == []

    async def test_a_target_that_simply_refuses_the_install_is_not_one(self, central_run):
        """The journal is for missing identities, not for every failure out here.

        A refused write, an unreachable host, an agent that dies — those are the
        central runtime's own failures, with their own owner. Writing them here
        would turn one administrator-facing signal about provisioning into a
        second QA error log, and the first one would stop being worth reading.
        """
        provisioning = RecordingProvisioningJournal()
        conn = FakeConn()

        async def refuse(command, *, check=False, timeout=None):
            conn.commands.append(command)
            return SimpleNamespace(exit_status=1, stdout="", stderr="Permission denied")

        conn.run = refuse

        async def behaviour(graph):
            return PASSING_JSON

        result, _, _, _ = await central_run(
            behaviour=behaviour, conn=conn, provisioning=provisioning
        )

        assert result.passed is False
        assert provisioning.entries == []

    async def test_a_revoke_that_cannot_be_read_back_is_residue_not_success(self):
        """Silence is not "the key is gone"; it is a record the sweep keeps."""
        conn = FakeConn()

        async def unreadable(command, *, check=False, timeout=None):
            conn.commands.append(command)
            return SimpleNamespace(exit_status=255, stdout="", stderr="broken pipe")

        with patch("src.consumers._qa_target._import", lambda key: key):
            conn.run = unreadable
            with patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)):
                residual = await revoke_grant(
                    server_ip="1.2.3.4",
                    ssh_user="root",
                    qa_ssh_user="qa-observer",
                    fleet_key="fleet-key",
                    marker="codegen-qa-run-abc",
                )

        assert residual is not None
        assert "codegen-qa-run-abc" in residual


class TestCapabilitySet:
    """The set is resolved from the deployment, not guessed by a tool."""

    async def test_root_is_what_the_target_resolves_and_containers_are_what_docker_says(self):
        conn = FakeConn()

        capabilities = await resolve_capabilities(conn, TARGET)

        assert capabilities.physical_root == PHYSICAL_ROOT
        assert capabilities.containers == frozenset(OWN_CONTAINERS)
        assert capabilities.loopback_ports == frozenset({8000, 8001})
        assert capabilities.deployed_url == TARGET.deployed_url
        listing = next(c for c in conn.commands if f"{QA_DOCKER_WRAPPER} ps" in c)
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
        assert conn.run_keys == []


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
            return SimpleNamespace(
                exit_status=0,
                stdout=(
                    'telegram_probe_result:{"action":"message","attempted":"send /start '
                    'to @weather_bot","sent":"/start","delivered":true,"replies":[{"id":7,'
                    '"text":"hi","caption":null,"media_type":null,"reply_markup":null}],'
                    '"callback":null,"error":null}\n'
                ),
                stderr="",
            )

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

        assert answer["replies"][0]["text"] == "hi"
        assert "@weather_bot" in sent[0]
        assert "@weather_bot" in tools["telegram_probe"].description

    async def test_a_visible_inline_button_is_invoked_only_through_this_run(self, tmp_path):
        from src.agents.qa.tools import build_qa_tools

        with_bot = QACapabilities(
            deployed_url=TARGET.deployed_url,
            physical_root=PHYSICAL_ROOT,
            containers=frozenset(OWN_CONTAINERS),
            loopback_ports=frozenset({8000}),
            bot_username="weather_bot",
        )
        scripts: list[str] = []

        async def probe(script, *, env, timeout):
            scripts.append(script)
            if "GetBotCallbackAnswerRequest" in script:
                return SimpleNamespace(
                    exit_status=0,
                    stdout=(
                        'telegram_probe_result:{"action":"callback","attempted":"press '
                        'Details","sent":"message_id=7 callback_data=ZGV0YWlscw==",'
                        '"delivered":true,"replies":[{"id":8,"text":"Forecast details",'
                        '"caption":null,"media_type":null,"reply_markup":null}],"callback":'
                        '{"text":"opened","alert":false,"url":null},"error":null}\n'
                    ),
                    stderr="",
                )
            return SimpleNamespace(
                exit_status=0,
                stdout=(
                    'telegram_probe_result:{"action":"message","attempted":"send /start '
                    'to @weather_bot","sent":"/start","delivered":true,"replies":[{"id":7,'
                    '"text":"Choose","caption":null,"media_type":null,"reply_markup":{"type":'
                    '"ReplyInlineMarkup","buttons":[{"row":0,"column":0,"text":"Details",'
                    '"type":"KeyboardButtonCallback","callback_data":"ZGV0YWlscw=="}]}}],'
                    '"callback":null,"error":null}\n'
                ),
                stderr="",
            )

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
            first = await tools["telegram_probe"].ainvoke({"message": "/start"})
            callback = await tools["telegram_click_button"].ainvoke(
                {"message_id": first["replies"][0]["id"], "callback_data": "ZGV0YWlscw=="}
            )

        assert callback["callback"]["text"] == "opened"
        assert callback["replies"][0]["text"] == "Forecast details"
        assert "GetBotCallbackAnswerRequest" in scripts[-1]

    async def test_an_unseen_inline_button_becomes_a_non_product_blocker(self, tmp_path):
        from src.agents.qa.tools import build_qa_callables

        with_bot = QACapabilities(
            deployed_url=TARGET.deployed_url,
            physical_root=PHYSICAL_ROOT,
            containers=frozenset(OWN_CONTAINERS),
            loopback_ports=frozenset({8000}),
            bot_username="weather_bot",
        )

        with qa_workspace(root=str(tmp_path)) as workspace:
            calls = build_qa_callables(
                session=_session(capabilities=with_bot),
                workspace=workspace,
                telethon_env={"TELETHON_SESSION": "s"},
            )
            answer = await calls["telegram_click_button"](7, "ZGV0YWlscw==")

            assert answer["delivered"] is False
            assert workspace.telegram_probe_blocker is not None
            assert (
                workspace.telegram_probe_blocker.category
                is QABlockerCategory.TELEGRAM_PROBE_UNDELIVERED
            )

    async def test_a_pre_delivery_telegram_error_blocks_the_agent_verdict(self, central_run):
        runtime = QARuntimeConfig(
            executor_agent_type=AgentType.CLAUDE,
            capability_host="127.0.0.1",
            telethon_env={"TELETHON_SESSION": "s"},
        )

        async def behaviour(harness):
            answer = await harness.tools["telegram_probe"].ainvoke({"message": ""})
            assert "error" in answer
            return (
                '{"pass": false, "checks": [{"name": "Telegram /start", '
                '"pass": false, "detail": "empty"}], "summary": "bot failed"}'
            )

        async def probe(script, *, env, timeout):
            return SimpleNamespace(
                exit_status=0,
                stdout=(
                    'telegram_probe_result:{"action":"message","attempted":"send \'\' '
                    'to @weather_bot","sent":"","delivered":false,"replies":[], '
                    '"callback":null,"error":"ValueError: The message cannot be empty"}\n'
                ),
                stderr="",
            )

        with patch("src.agents.qa.tools.run_probe_script", probe):
            result, _, _, _ = await central_run(
                behaviour=behaviour,
                runtime=runtime,
                target=replace(TARGET, bot_username="weather_bot"),
            )

        assert result.blocker is not None
        assert result.blocker.category is QABlockerCategory.TELEGRAM_PROBE_UNDELIVERED
        assert result.telegram_probe_evidence[0].delivered is False

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

        # Not `docker top` — the QA account cannot reach the daemon. It asks the
        # wrapper provisioning installed, which is where the target refuses the
        # sub-commands that write.
        assert conn.commands == [f"{' '.join(QA_DOCKER)} top {OWN_CONTAINERS[0]}"]

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
            patch("src.consumers._qa_runner.run_qa_executor", _executor_factory(behaviour)),
        ):
            result = await run_qa_centrally(
                target=TARGET,
                ownership=WorkerOwnership(
                    project_id="proj-qa", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
                ),
                fleet_ssh_key="fleet-key",
                acceptance_criteria="- GET /health returns 200",
                runtime=RUNTIME,
                grant_journal=journal,
                provisioning_journal=RecordingProvisioningJournal(),
                settings=NO_API_FALLBACK,
                established_facts=[],
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
        assert conn.run_keys == []
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
                    assert conn.run_keys
                    raise RuntimeError("cancelled")

        assert outcome.revoked is True
        assert conn.run_keys == []
        # The line provisioning opened the file with is still there: the run was
        # a guest in that file, not its owner.
        assert conn.authorized_keys == [SENTINEL]
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
            def _revoke(self, user, marker):
                return SimpleNamespace(exit_status=0, stdout="1\n", stderr="")

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
