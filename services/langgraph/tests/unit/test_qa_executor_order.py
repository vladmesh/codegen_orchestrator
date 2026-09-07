"""Who performs exploratory QA, and what happens when that executor cannot run.

The contract this file encodes:

1. QA has exactly one executor — the assigned subscription coding agent — and
   only `claude` or `codex` may be assigned to it;
2. a transient failure to start it is retried once; a failure that says the
   session is not there is not retried, because a second attempt cannot make a
   session exist;
3. when it still does not run, the run ends there, as a typed QA-infrastructure
   outcome naming the executor — never as a product verdict, and never as an
   LLM retry of the same testing.
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
        # A read of a file this deployment does not have. It carries no kit
        # package contract, so the run states nothing about packages — which is
        # what these tests' deployment is.
        if command.startswith("sh -c") and "head -c" in command:
            return SimpleNamespace(exit_status=5, stdout="", stderr="notafile")
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


async def _run(
    *,
    executor,
    runtime=CLAUDE_RUNTIME,
    tmp_path,
    acceptance_criteria="- GET /health returns 200",
):
    with (
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=FakeConn())),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
        patch("src.consumers._qa_runner.run_qa_executor", executor),
    ):
        return await _invoke(runtime, acceptance_criteria=acceptance_criteria)


async def _invoke(runtime, *, acceptance_criteria):
    return await run_qa_centrally(
        target=TARGET,
        ownership=OWNERSHIP,
        fleet_ssh_key="fleet-key",
        acceptance_criteria=acceptance_criteria,
        runtime=runtime,
        grant_journal=Journal(),
        provisioning_journal=ProvisioningJournal(),
        established_facts=[],
    )


class TestTheAssignedExecutorGoesFirst:
    async def test_runner_logs_criterion_adjustments_with_its_qa_run_id(self, tmp_path):
        executor = _submitting_executor()
        criteria = "* Confirm POST `/api/settings/get` returns 200 THEN GET /preferences shows it"

        with patch("src.consumers._qa_runner.logger") as logger:
            await _run(executor=executor, tmp_path=tmp_path, acceptance_criteria=criteria)

        adjustment_call = next(
            call
            for call in logger.info.call_args_list
            if call.args == ("qa_platform_owned_criteria_adjusted",)
        )
        assert adjustment_call.kwargs["qa_run_id"] == OWNERSHIP.attempt_id
        assert adjustment_call.kwargs["adjustments"][0]["rewritten"] == (
            "* With the deployment-established setting, verify: GET /preferences shows it"
        )
        assert "POST `/api/settings/get`" not in executor.calls[0]["prompt"]

    async def test_codex_is_the_default_executor(self, tmp_path):
        executor = _submitting_executor()

        result = await _run(executor=executor, runtime=CODEX_RUNTIME, tmp_path=tmp_path)

        assert result.passed is True
        assert executor.calls[0]["agent_type"] is AgentType.CODEX

    async def test_codex_runs_when_it_is_the_one_assigned(self, tmp_path):
        executor = _submitting_executor()

        await _run(executor=executor, runtime=CODEX_RUNTIME, tmp_path=tmp_path)

        assert executor.calls[0]["agent_type"] is AgentType.CODEX

    async def test_a_codex_executor_never_hands_its_profile_or_key_to_the_target(self, tmp_path):
        executor = _submitting_executor()

        await _run(executor=executor, runtime=CODEX_RUNTIME, tmp_path=tmp_path)

        handed_to_executor = json.dumps(
            {key: str(value) for key, value in executor.calls[0].items()}
        )
        for credential in ("CODEX_HOME", "auth.json", "OPENAI_API_KEY", "CODEX_API_KEY"):
            assert credential not in handed_to_executor

    async def test_the_executor_is_handed_an_endpoint_and_nothing_else(self, tmp_path):
        executor = _submitting_executor()

        await _run(executor=executor, tmp_path=tmp_path)

        [call] = executor.calls
        handed = json.dumps({k: str(v) for k, v in call.items()})
        assert call["capability_url"].startswith("http://127.0.0.1:")
        assert call["capability_token"]
        # The deployed URL is deliberately there — a black-box tester has to
        # know what it is testing, and the container can reach the internet
        # anyway. Everything that would let it act as the platform is not.
        for secret in ("fleet-key", "BEGIN OPENSSH", "TELETHON", "qa-observer", "vps-1"):
            assert secret not in handed


class TestAnExecutorThatCannotStart:
    async def test_a_transient_failure_is_retried_within_a_named_bound(self, tmp_path):
        executor = _failing_executor(
            QAExecutorUnavailable("host was busy", transient=True),
            QAExecutorUnavailable("host was busy", transient=True),
        )

        result = await _run(executor=executor, tmp_path=tmp_path)

        assert len(executor.attempts) == QA_EXECUTOR_ATTEMPTS
        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE

    async def test_a_missing_session_is_not_retried(self, tmp_path):
        """Configuration does not become present by being asked for twice."""
        executor = _failing_executor(
            QAExecutorUnavailable(
                "CLAUDE_CONFIG_DIR is not a mounted host directory", transient=False
            )
        )

        await _run(executor=executor, tmp_path=tmp_path)

        assert len(executor.attempts) == 1

    @pytest.mark.parametrize("runtime", [CLAUDE_RUNTIME, CODEX_RUNTIME])
    async def test_an_executor_that_does_not_run_ends_the_run_as_infrastructure(
        self, tmp_path, runtime
    ):
        """There is no second executor. The run stops, typed, naming the first.

        This is the whole failure behaviour: no LLM is asked to repeat the
        testing on an API key, so what an administrator gets is which executor
        was assigned and why it did not start.
        """
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable("CLAUDE_CONFIG_DIR is not mounted", transient=False)
            ),
            runtime=runtime,
            tmp_path=tmp_path,
        )

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE
        # The alert has to name the executor that was assigned and what it said.
        assert runtime.executor_agent_type.value in result.blocker.attempted
        assert runtime.executor_agent_type.value in result.blocker.sent
        assert "CLAUDE_CONFIG_DIR is not mounted" in result.blocker.received
        # And this is not a product judgement: no failed checks to fix.
        assert result.checks == []

    async def test_an_executor_that_ran_and_said_nothing_useful_keeps_what_it_said(self, tmp_path):
        """It started, it emitted output, it never called the endpoint.

        Its container is deleted by the time this returns and worker-wrapper
        retains no transcript for a QA executor, so this result is the last
        place that account exists. Both attempts ran here, and both are kept
        under their own header.
        """
        transcript = '{"output": "I could not reach the capability endpoint"}'
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable(
                    "the QA executor container ran but never reached the capability endpoint: "
                    f"{transcript}",
                    transient=True,
                    transcript=transcript,
                ),
            ),
            tmp_path=tmp_path,
        )

        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE
        assert result.executor_evidence == (
            f"== QA executor attempt 1 of {QA_EXECUTOR_ATTEMPTS} ==\n{transcript}\n"
            f"== QA executor attempt 2 of {QA_EXECUTOR_ATTEMPTS} ==\n{transcript}"
        )

    async def test_a_later_attempt_with_nothing_to_say_does_not_erase_the_first(self, tmp_path):
        """The reviewer's sequence: attempt one ran and spoke, attempt two never started.

        The retry used to keep only the last failure, so the run retained
        nothing at all — the one transcript that existed was overwritten by an
        attempt that had none. Every attempt that ran is retained, delimited, in
        the order they ran.
        """
        spoke = '{"output": "first executor reached no endpoint"}'
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable(
                    f"the QA executor container ran but never reached the endpoint: {spoke}",
                    transient=True,
                    transcript=spoke,
                ),
                QAExecutorUnavailable(
                    "worker-manager did not acknowledge the QA executor within 120s",
                    transient=True,
                ),
            ),
            tmp_path=tmp_path,
        )

        assert result.executor_evidence == (
            f"== QA executor attempt 1 of {QA_EXECUTOR_ATTEMPTS} ==\n{spoke}"
        )

    async def test_an_executor_that_never_started_records_no_transcript(self, tmp_path):
        """No attempt ran, so this record holds none — which claims nothing further."""
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable("CLAUDE_CONFIG_DIR is not mounted", transient=False)
            ),
            tmp_path=tmp_path,
        )

        assert result.executor_evidence is None

    async def test_no_llm_configuration_is_offered_as_the_remedy(self, tmp_path):
        """QA has one executor; the outcome must not point at a removed fallback."""
        result = await _run(
            executor=_failing_executor(QAExecutorUnavailable("no session", transient=False)),
            tmp_path=tmp_path,
        )

        blocker = json.dumps(result.blocker.model_dump())
        assert "QA_LLM" not in blocker
        assert "fallback" not in blocker.lower()


class TestAnExecutorThatRanButSaidNothing:
    async def test_a_silent_executor_is_not_an_executor_failure(self, tmp_path):
        """It ran. There is simply no answer, which is an unknown result, not a start failure."""

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

        result = await _run(executor=ran_and_said_nothing, tmp_path=tmp_path)

        assert result.passed is False
        assert result.blocker.category is QABlockerCategory.UNKNOWN
        # It ran and said nothing, and that is what the run records: the empty
        # string, never a header this code assembled around nothing.
        assert result.executor_evidence == ""

    async def test_silence_across_every_attempt_stays_silence(self, tmp_path):
        """Two containers ran, both said nothing, and neither reached the endpoint.

        The retry must not turn that into content. What the run records is the
        runner's own observation — an executor ran and produced no output — and
        the artifact states that absence rather than publishing an attempt
        header as though an agent had written it.
        """
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable(
                    "the QA executor container ran but never reached the endpoint: no output",
                    transient=True,
                    transcript="",
                ),
            ),
            tmp_path=tmp_path,
        )

        assert result.blocker.category is QABlockerCategory.QA_EXECUTOR_UNAVAILABLE
        assert result.executor_evidence == ""

    async def test_a_silent_attempt_beside_a_speaking_one_adds_no_empty_section(self, tmp_path):
        """Only the attempt that produced output is retained, under its own header."""
        spoke = '{"output": "second executor reached no endpoint"}'
        result = await _run(
            executor=_failing_executor(
                QAExecutorUnavailable("ran, said nothing", transient=True, transcript=""),
                QAExecutorUnavailable(
                    f"ran but never reached the endpoint: {spoke}",
                    transient=True,
                    transcript=spoke,
                ),
            ),
            tmp_path=tmp_path,
        )

        assert result.executor_evidence == (
            f"== QA executor attempt 2 of {QA_EXECUTOR_ATTEMPTS} ==\n{spoke}"
        )


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
