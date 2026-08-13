"""Who performs exploratory QA, in what order, and what happens when nobody can.

The contract this file encodes:

1. the assigned subscription coding agent is tried first, and a run it performs
   never reads `QA_LLM_*` at all;
2. the API triplet is consulted only after that executor has actually failed to
   run, and only then;
3. a transient failure is retried once; a failure that says the session is not
   there is not retried, because a second attempt cannot make a session exist;
4. with no fallback configured, the run ends as a typed QA-infrastructure
   outcome — never as a product verdict.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from pydantic import ValidationError
import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src.clients.qa_worker import QAExecutorRun, QAExecutorUnavailable
from src.config.settings import Settings
from src.consumers._qa_runner import (
    QA_EXECUTOR_ATTEMPTS,
    QARuntimeConfig,
    api_fallback,
    run_qa_centrally,
)
from src.consumers._qa_target import QATarget

TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="root",
    qa_ssh_user="qa-observer",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000}),
)
OWNERSHIP = WorkerOwnership(
    project_id="proj-weather", run_id="qa-run-1", attempt_id="attempt-qa-run-1"
)
PHYSICAL_ROOT = "/srv/deployments/weather-bot"
CONTAINER = "weather-bot-backend-1"
RUNNING_STATE = (
    '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0,'
    '"Health":{"Status":"healthy"}}'
)
PASSING_JSON = '{"pass": true, "checks": [], "summary": "OK"}'

CLAUDE_RUNTIME = QARuntimeConfig(executor_agent_type=AgentType.CLAUDE, capability_host="127.0.0.1")
CODEX_RUNTIME = QARuntimeConfig(executor_agent_type=AgentType.CODEX, capability_host="127.0.0.1")

NO_FALLBACK = SimpleNamespace(qa_llm_model=None, qa_llm_base_url=None, qa_llm_api_key=None)
PARTIAL_FALLBACK = SimpleNamespace(
    qa_llm_model="m", qa_llm_base_url="http://llm.invalid/v1", qa_llm_api_key=None
)
FULL_FALLBACK = SimpleNamespace(
    qa_llm_model="m", qa_llm_base_url="http://llm.invalid/v1", qa_llm_api_key="k"
)


class ForbiddenSettings:
    """Settings that make reading the API triplet a test failure.

    `QA_LLM_*` may not be read because a run started. A run whose executor works
    must never touch these, and this is how that is proven rather than asserted
    in prose.
    """

    def __init__(self) -> None:
        self.reads: list[str] = []

    def __getattr__(self, name: str):
        self.reads.append(name)
        raise AssertionError(f"{name} was read before the assigned executor failed")


class FakeConn:
    """A target that answers the grant and capability commands."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        if command.startswith("readlink -f --"):
            return SimpleNamespace(exit_status=0, stdout=f"{PHYSICAL_ROOT}\n", stderr="")
        if " ps " in command:
            return SimpleNamespace(exit_status=0, stdout=f"{CONTAINER}\n", stderr="")
        # The container-state probe the runner performs before any executor
        # starts. This deployment is up, so who runs QA is what these tests are
        # left deciding.
        if " inspect " in command:
            return SimpleNamespace(exit_status=0, stdout=RUNNING_STATE, stderr="")
        if "grep -c -F" in command:
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Journal:
    def __init__(self) -> None:
        self.states = []

    async def write(self, grant) -> None:
        self.states.append(grant.state)


class ProvisioningJournal:
    def __init__(self) -> None:
        self.entries = []

    async def missing_identity(self, *, reason, detail) -> None:
        self.entries.append((reason, detail))


def _submitting_executor(verdict: str = PASSING_JSON):
    """An executor that answers, holding only the endpoint URL and its token."""
    calls: list[dict] = []

    async def run(**kwargs):
        calls.append(kwargs)
        async with aiohttp.ClientSession() as http:
            await http.post(
                kwargs["capability_url"],
                json={"tool": "submit_qa_result", "args": {"result": verdict}},
                headers={"Authorization": f"Bearer {kwargs['capability_token']}"},
            )
        return QAExecutorRun(
            verdict_submitted=kwargs["verdict_received"].is_set(),
            calls_served=kwargs["calls_served"](),
            detail="test",
        )

    run.calls = calls
    return run


def _failing_executor(*failures: QAExecutorUnavailable):
    """An executor that fails as scripted, recording each attempt."""
    attempts: list[dict] = []
    queue = list(failures)

    async def run(**kwargs):
        attempts.append(kwargs)
        raise queue.pop(0) if queue else failures[-1]

    run.attempts = attempts
    return run


async def _run(*, executor, settings, runtime=CLAUDE_RUNTIME, tmp_path, graph=None):
    patches = [
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=FakeConn())),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
        patch("src.consumers._qa_runner.run_qa_executor", executor),
    ]
    if graph is not None:
        patches.append(patch("src.consumers._qa_runner.create_qa_graph", graph))
    with patches[0], patches[1], patches[2], patches[3]:
        if graph is not None:
            with patches[4]:
                return await _invoke(runtime, settings)
        return await _invoke(runtime, settings)


async def _invoke(runtime, settings):
    return await run_qa_centrally(
        target=TARGET,
        ownership=OWNERSHIP,
        fleet_ssh_key="fleet-key",
        acceptance_criteria="- GET /health returns 200",
        runtime=runtime,
        grant_journal=Journal(),
        provisioning_journal=ProvisioningJournal(),
        settings=settings,
        established_facts=[],
    )


class TestTheAssignedExecutorGoesFirst:
    async def test_codex_is_the_default_executor(self, tmp_path):
        executor = _submitting_executor()

        result = await _run(
            executor=executor, settings=NO_FALLBACK, runtime=CODEX_RUNTIME, tmp_path=tmp_path
        )

        assert result.passed is True
        assert executor.calls[0]["agent_type"] is AgentType.CODEX

    async def test_codex_runs_when_it_is_the_one_assigned(self, tmp_path):
        executor = _submitting_executor()

        await _run(
            executor=executor,
            settings=NO_FALLBACK,
            runtime=CODEX_RUNTIME,
            tmp_path=tmp_path,
        )

        assert executor.calls[0]["agent_type"] is AgentType.CODEX

    async def test_a_codex_executor_never_hands_its_profile_or_key_to_the_target(self, tmp_path):
        executor = _submitting_executor()

        await _run(
            executor=executor, settings=NO_FALLBACK, runtime=CODEX_RUNTIME, tmp_path=tmp_path
        )

        handed_to_executor = json.dumps(
            {key: str(value) for key, value in executor.calls[0].items()}
        )
        for credential in ("CODEX_HOME", "auth.json", "OPENAI_API_KEY", "CODEX_API_KEY"):
            assert credential not in handed_to_executor

    async def test_a_working_executor_never_reads_the_api_triplet(self, tmp_path):
        """AC2: the triplet is not read because a run happened."""
        settings = ForbiddenSettings()

        result = await _run(executor=_submitting_executor(), settings=settings, tmp_path=tmp_path)

        assert result.passed is True
        assert settings.reads == []

    async def test_the_executor_is_handed_an_endpoint_and_nothing_else(self, tmp_path):
        executor = _submitting_executor()

        await _run(executor=executor, settings=NO_FALLBACK, tmp_path=tmp_path)

        [call] = executor.calls
        handed = json.dumps({k: str(v) for k, v in call.items()})
        assert call["capability_url"].startswith("http://127.0.0.1:")
        assert call["capability_token"]
        # The deployed URL is deliberately there — a black-box tester has to
        # know what it is testing, and the container can reach the internet
        # anyway. Everything that would let it act as the platform is not.
        for secret in ("fleet-key", "BEGIN OPENSSH", "TELETHON", "qa-observer", "vps-1"):
            assert secret not in handed


class TestFallbackOnlyAfterAnActualFailure:
    async def test_a_transient_failure_is_retried_within_a_named_bound(self, tmp_path):
        executor = _failing_executor(
            QAExecutorUnavailable("host was busy", transient=True),
            QAExecutorUnavailable("host was busy", transient=True),
        )

        result = await _run(executor=executor, settings=NO_FALLBACK, tmp_path=tmp_path)

        assert len(executor.attempts) == QA_EXECUTOR_ATTEMPTS
        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE

    async def test_a_missing_session_is_not_retried(self, tmp_path):
        """Configuration does not become present by being asked for twice."""
        executor = _failing_executor(
            QAExecutorUnavailable(
                "CLAUDE_CONFIG_DIR is not a mounted host directory", transient=False
            )
        )

        await _run(executor=executor, settings=NO_FALLBACK, tmp_path=tmp_path)

        assert len(executor.attempts) == 1

    async def test_a_full_triplet_continues_the_run_through_the_api(self, tmp_path):
        class FallbackGraph:
            async def ainvoke(self, state, config=None):
                return {"messages": [SimpleNamespace(content=PASSING_JSON)]}

        built = {}

        def create(*, model, base_url, api_key, tools, prompt):
            built.update(model=model, base_url=base_url, api_key=api_key)
            return FallbackGraph()

        result = await _run(
            executor=_failing_executor(QAExecutorUnavailable("no session", transient=False)),
            settings=FULL_FALLBACK,
            tmp_path=tmp_path,
            graph=create,
        )

        assert result.passed is True
        assert built == {"model": "m", "base_url": "http://llm.invalid/v1", "api_key": "k"}

    @pytest.mark.parametrize("settings", [NO_FALLBACK, PARTIAL_FALLBACK])
    async def test_without_a_complete_triplet_the_outcome_is_infrastructure(
        self, tmp_path, settings
    ):
        result = await _run(
            executor=_failing_executor(QAExecutorUnavailable("no session", transient=False)),
            settings=settings,
            tmp_path=tmp_path,
        )

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE
        # The alert has to be able to say what is missing.
        assert "QA_LLM_API_KEY" in result.blocker.sent
        # And this is not a product judgement: no failed checks to fix.
        assert result.checks == []


class TestAnExecutorThatRanButSaidNothing:
    async def test_a_silent_executor_is_not_an_executor_failure(self, tmp_path):
        """It ran. There is simply no answer, which is a human's problem, not a fallback's."""

        async def ran_and_said_nothing(**kwargs):
            async with aiohttp.ClientSession() as http:
                await http.post(
                    kwargs["capability_url"],
                    json={"tool": "container_logs", "args": {"container": CONTAINER}},
                    headers={"Authorization": f"Bearer {kwargs['capability_token']}"},
                )
            return QAExecutorRun(
                verdict_submitted=False, calls_served=kwargs["calls_served"](), detail="test"
            )

        settings = ForbiddenSettings()
        result = await _run(executor=ran_and_said_nothing, settings=settings, tmp_path=tmp_path)

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.UNKNOWN
        # No fallback was considered: an executor did run.
        assert settings.reads == []


class TestApiFallbackResolution:
    def test_a_complete_triplet_resolves(self):
        fallback = api_fallback(FULL_FALLBACK)

        assert fallback.model == "m"

    @pytest.mark.parametrize("settings", [NO_FALLBACK, PARTIAL_FALLBACK])
    def test_an_incomplete_triplet_is_simply_no_fallback(self, settings):
        assert api_fallback(settings) is None


class TestOnlyAnAssignedSubscriptionAgentCanBeConfigured:
    """`QA_EXECUTOR_AGENT_TYPE` names one of two agents, or the service refuses.

    This is the first of the two places the executor is fixed — the other being
    the create command worker-manager validates. An operator who writes
    `factory` here would have QA run on a provider API key, and one who writes
    `noop` would have a QA run that performs no testing; both are refused when
    the configuration is read, so neither can turn into a run.
    """

    def test_codex_is_the_default(self):
        assert Settings().qa_executor_agent_type is AgentType.CODEX

    @pytest.mark.parametrize("assigned", ["claude", "codex"])
    def test_an_assigned_subscription_agent_is_accepted(self, assigned, monkeypatch):
        monkeypatch.setenv("QA_EXECUTOR_AGENT_TYPE", assigned)

        assert Settings().qa_executor_agent_type is AgentType(assigned)

    @pytest.mark.parametrize("rejected", ["factory", "noop"])
    def test_no_other_agent_can_be_assigned(self, rejected, monkeypatch):
        monkeypatch.setenv("QA_EXECUTOR_AGENT_TYPE", rejected)

        with pytest.raises(ValidationError):
            Settings()

    @pytest.mark.parametrize("rejected", [AgentType.FACTORY, AgentType.NOOP])
    def test_the_same_holds_when_it_is_passed_directly(self, rejected):
        with pytest.raises(ValidationError):
            Settings(qa_executor_agent_type=rejected)

    @pytest.mark.parametrize("agent", ["factory", "noop"])
    def test_a_developer_worker_still_accepts_every_agent(self, agent, monkeypatch):
        """The narrowing is on QA alone: the default developer agent is untouched."""
        monkeypatch.setenv("DEFAULT_AGENT_TYPE", agent)

        assert Settings().default_agent_type is AgentType(agent)
