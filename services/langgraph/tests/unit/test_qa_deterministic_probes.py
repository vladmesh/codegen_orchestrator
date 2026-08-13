"""The two facts QA establishes itself, before any model is spent on them.

Container state and "does the bot answer getMe" used to be discovered by the
exploratory agent, or not at all. Both are deterministic — one docker read over
the run's own session, one getMe the API makes with the token it already holds —
so both are established here, before an executor exists, and the run is told what
they said instead of being asked to find out.

Each probe is covered three ways, because the three outcomes go to three
different places:

* it holds — the fact is handed to the executor and struck off its checklist;
* the product is at fault — a container is down, or Telegram refuses the bot's
  token. Deterministic, no LLM, and never handed to one;
* the infrastructure did not answer — docker, Telegram or the platform API. That
  is retried, then reported as a QA-infrastructure outcome with an
  administrator alert, through the mechanism that already exists for an
  executor that could not be started.
"""

from __future__ import annotations

from contextlib import ExitStack
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import httpx
import pytest

from shared.contracts.dto.run_result import QABlockerCategory
from shared.contracts.dto.telegram import BotLiveness, BotLivenessState
from shared.contracts.queues.qa import QAOutcome, QAServerInfo
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src.clients.qa_worker import QAExecutorRun
from src.consumers._qa_runner import (
    QAResult,
    QARuntimeConfig,
    _ContainerStateUnreadable,
    read_container_state,
    run_qa_centrally,
)
from src.consumers._qa_target import CONTAINER_PROBE_ATTEMPTS, QATarget
from src.consumers.qa import (
    BOT_LIVENESS_ATTEMPTS,
    BOT_LIVENESS_MAX_RETRY_DELAY,
    process_qa_job,
)

TARGET = QATarget(
    server_ip="1.2.3.4",
    ssh_user="root",
    qa_ssh_user="qa-observer",
    server_handle="vps-1",
    project_name="weather-bot",
    deployed_url="http://1.2.3.4:8000",
    allocated_ports=frozenset({8000}),
)
PHYSICAL_ROOT = "/srv/deployments/weather-bot"
CONTAINERS = ("weather-bot-backend-1", "weather-bot-db-1")
RUNTIME = QARuntimeConfig(executor_agent_type=AgentType.CLAUDE, capability_host="127.0.0.1")
NO_API_FALLBACK = SimpleNamespace(qa_llm_model=None, qa_llm_base_url=None, qa_llm_api_key=None)
PASSING_JSON = '{"pass": true, "checks": [], "summary": "OK"}'

RUNNING = '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0}'
HEALTHY = (
    '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0,'
    '"Health":{"Status":"healthy"}}'
)
EXITED = '{"Status":"exited","Running":false,"Restarting":false,"ExitCode":137}'
RESTARTING = '{"Status":"restarting","Running":false,"Restarting":true,"ExitCode":1}'
UNHEALTHY = (
    '{"Status":"running","Running":true,"Restarting":false,"ExitCode":0,'
    '"Health":{"Status":"unhealthy"}}'
)


class FakeConn:
    """A target answering the grant, capability and container-state commands."""

    def __init__(
        self,
        *,
        containers: tuple[str, ...] = CONTAINERS,
        states: dict[str, str] | None = None,
        inspect_exit: int = 0,
        inspect_stderr: str = "",
        ps_exit: int = 0,
        ps_stderr: str = "",
        readlink_exit: int = 0,
    ) -> None:
        self.commands: list[str] = []
        self.containers = containers
        self.states = states or dict.fromkeys(containers, HEALTHY)
        self.inspect_exit = inspect_exit
        self.inspect_stderr = inspect_stderr
        self.ps_exit = ps_exit
        self.ps_stderr = ps_stderr
        self.readlink_exit = readlink_exit

    async def run(self, command, *, check=False, timeout=None):
        self.commands.append(command)
        if command.startswith("readlink -f --"):
            if self.readlink_exit:
                return SimpleNamespace(
                    exit_status=self.readlink_exit, stdout="", stderr="no such directory"
                )
            return SimpleNamespace(exit_status=0, stdout=f"{PHYSICAL_ROOT}\n", stderr="")
        if " ps " in command:
            if self.ps_exit:
                return SimpleNamespace(exit_status=self.ps_exit, stdout="", stderr=self.ps_stderr)
            return SimpleNamespace(
                exit_status=0, stdout="".join(f"{c}\n" for c in self.containers), stderr=""
            )
        if " inspect " in command:
            if self.inspect_exit:
                return SimpleNamespace(
                    exit_status=self.inspect_exit, stdout="", stderr=self.inspect_stderr
                )
            return SimpleNamespace(
                exit_status=0, stdout=self.states[shlex.split(command)[-1]], stderr=""
            )
        if "grep -c -F" in command:
            return SimpleNamespace(exit_status=0, stdout="0\n", stderr="")
        return SimpleNamespace(exit_status=0, stdout="", stderr="")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def inspected(self) -> list[str]:
        return [c for c in self.commands if " inspect " in c]

    @property
    def listed(self) -> list[str]:
        return [c for c in self.commands if " ps " in c]


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


def _recording_executor():
    """An executor that answers, and records that it was started at all."""
    calls: list[dict] = []

    async def run(**kwargs):
        calls.append(kwargs)
        async with aiohttp.ClientSession() as http:
            await http.post(
                kwargs["capability_url"],
                json={"tool": "submit_qa_result", "args": {"result": PASSING_JSON}},
                headers={"Authorization": f"Bearer {kwargs['capability_token']}"},
            )
        return QAExecutorRun(
            verdict_submitted=kwargs["verdict_received"].is_set(),
            calls_served=kwargs["calls_served"](),
            detail="test executor",
        )

    run.calls = calls
    return run


async def _run_qa(conn: FakeConn, executor, tmp_path, established_facts=None):
    with (
        patch("src.consumers._qa_target._connect", AsyncMock(return_value=conn)),
        patch("src.consumers._qa_target._import", lambda key: key),
        patch("src.consumers._qa_workspace.QA_WORKSPACE_ROOT", str(tmp_path / "qa-runs")),
        patch("src.consumers._qa_runner.run_qa_executor", executor),
        patch("src.consumers._qa_runner.CONTAINER_PROBE_RETRY_DELAY", 0),
        patch("src.consumers._qa_target.CONTAINER_PROBE_RETRY_DELAY", 0),
    ):
        return await run_qa_centrally(
            target=TARGET,
            ownership=WorkerOwnership(project_id="proj-qa", run_id="qa-run-1"),
            fleet_ssh_key="fleet-key",
            acceptance_criteria="- the bot answers /start",
            runtime=RUNTIME,
            grant_journal=Journal(),
            provisioning_journal=ProvisioningJournal(),
            settings=NO_API_FALLBACK,
            established_facts=established_facts or [],
        )


class TestContainerStateRules:
    """What one `docker inspect --format {{json .State}}` answer means."""

    def test_a_running_container_with_no_health_check_is_up(self):
        state = read_container_state("web", RUNNING)
        assert state.ok is True
        assert state.detail == "running"

    def test_a_running_container_reports_its_health(self):
        assert read_container_state("web", HEALTHY).detail == "running, health healthy"

    def test_an_exited_container_is_down_and_keeps_its_exit_code(self):
        state = read_container_state("web", EXITED)
        assert state.ok is False
        assert "137" in state.detail

    def test_a_restarting_container_is_a_restart_loop(self):
        state = read_container_state("web", RESTARTING)
        assert state.ok is False
        assert "restarting" in state.detail

    def test_a_running_but_unhealthy_container_is_not_up(self):
        state = read_container_state("web", UNHEALTHY)
        assert state.ok is False
        assert "unhealthy" in state.detail

    def test_an_answer_that_is_not_a_container_state_is_not_a_verdict(self):
        """Unparseable output says nothing about the product, so it is not read as one."""
        for payload in ("", "Error: No such object: web", '{"Running": true}'):
            with pytest.raises(_ContainerStateUnreadable):
                read_container_state("web", payload)


class TestContainerStateIsEstablishedBeforeTheExecutor:
    async def test_a_running_deployment_is_told_to_the_executor_as_given(self, tmp_path):
        """The agent is not asked to rediscover what the runner just read."""
        conn = FakeConn()
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert result.passed is True
        assert len(conn.inspected) == len(CONTAINERS)
        prompt = executor.calls[0]["prompt"]
        assert "Already established" in prompt
        for container in CONTAINERS:
            assert f"container {container} is running" in prompt
        assert "already established above; do not check it again" in prompt

    async def test_a_container_that_is_down_fails_qa_without_starting_an_executor(self, tmp_path):
        """A deployment that is not running has failed, and no model is spent saying so."""
        conn = FakeConn(states={CONTAINERS[0]: HEALTHY, CONTAINERS[1]: EXITED})
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.passed is False
        # A product defect, not a blocker: this is what the engineering loop is for.
        assert result.blocker is None
        failed = [check for check in result.checks if not check["pass"]]
        assert [check["name"] for check in failed] == [f"container {CONTAINERS[1]} is running"]
        assert "137" in failed[0]["detail"]

    async def test_a_restart_loop_is_a_product_defect_too(self, tmp_path):
        conn = FakeConn(states={CONTAINERS[0]: RESTARTING, CONTAINERS[1]: HEALTHY})
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.passed is False
        assert result.blocker is None

    async def test_docker_not_answering_is_infrastructure_never_a_product_verdict(self, tmp_path):
        conn = FakeConn(inspect_exit=1, inspect_stderr="Cannot connect to the Docker daemon")
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category is QABlockerCategory.QA_PROBE_UNAVAILABLE
        assert "Cannot connect to the Docker daemon" in result.blocker.received
        # Nothing was concluded about the product, so nothing is reported as a check.
        assert result.checks == []

    async def test_docker_is_retried_before_the_run_is_given_up(self, tmp_path):
        conn = FakeConn(inspect_exit=1, inspect_stderr="daemon not ready")
        executor = _recording_executor()

        await _run_qa(conn, executor, tmp_path)

        assert len(conn.inspected) > 1

    async def test_docker_not_answering_the_first_listing_is_the_same_outcome(self, tmp_path):
        """The capability listing is a docker call too, and it fails the same way.

        `docker ps` runs before a session exists, so it is the first call that can
        find the daemon down. Classifying it as "the server is unavailable" —
        which is what it used to become — would give the identical condition a
        different category, no retries and no administrator alert purely because
        of which call arrived first.
        """
        conn = FakeConn(ps_exit=1, ps_stderr="Cannot connect to the Docker daemon")
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category is QABlockerCategory.QA_PROBE_UNAVAILABLE
        assert "Cannot connect to the Docker daemon" in result.blocker.received
        # Nothing was read about any container, so nothing is claimed about the product.
        assert result.checks == []
        assert conn.inspected == []

    async def test_the_first_listing_is_retried_like_every_other_docker_read(self, tmp_path):
        conn = FakeConn(ps_exit=1, ps_stderr="daemon not ready")
        executor = _recording_executor()

        await _run_qa(conn, executor, tmp_path)

        assert len(conn.listed) == CONTAINER_PROBE_ATTEMPTS

    async def test_the_deployment_directory_not_resolving_is_not_a_docker_outage(self, tmp_path):
        """The split cuts where the cause differs: this one never reaches docker.

        A deployment directory that does not resolve says nothing about the
        container runtime — no docker call was made — so it keeps the capability
        failure it always had rather than being reported as a probe the platform
        could not perform.
        """
        conn = FakeConn(readlink_exit=1)
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.blocker.category is QABlockerCategory.SERVER_UNAVAILABLE
        assert conn.listed == []

    async def test_a_deployment_with_no_containers_is_infrastructure(self, tmp_path):
        """Docker knowing no container of this compose project concludes nothing."""
        conn = FakeConn(containers=())
        executor = _recording_executor()

        result = await _run_qa(conn, executor, tmp_path)

        assert executor.calls == []
        assert result.blocker.category is QABlockerCategory.QA_PROBE_UNAVAILABLE
        assert conn.inspected == []


# --- The bot-liveness probe, from the consumer that owns the boundary ---------

QA_CRITERIA = "- the bot answers /start with a welcome"


def _server_info() -> QAServerInfo:
    return QAServerInfo(
        server_ip="1.2.3.4",
        ssh_user="root",
        qa_ssh_user="qa-observer",
        ssh_key="fleet-key",
        project_name="weather-bot",
        server_handle="vps-1",
        allocated_ports=frozenset({8000}),
    )


@pytest.fixture
def bot_message() -> dict:
    return {
        "story_id": "story-1",
        "project_id": "116c9678-5872-4ce5-8332-9a267ab27604",
        "telegram_chat_id": "12345",
        "deployed_url": "https://weather.example.com",
        "application_id": 1,
        "acceptance_criteria": QA_CRITERIA,
        "run_id": "qa-run-1",
        "bot_username": "weather_bot",
        "qa_attempt": 0,
    }


@pytest.fixture
def redis():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.set = AsyncMock(return_value=True)
    client.redis.delete = AsyncMock()
    return client


@pytest.fixture
def api():
    """The API client the consumer holds — including the liveness question."""
    from shared.contracts.dto.deploy_dispatch import DeployRunStart
    from shared.contracts.dto.run import RunStatus

    client = AsyncMock()
    client.patch = AsyncMock(return_value={})
    client.get_project = AsyncMock(return_value=SimpleNamespace(slug="weather-bot", config={}))
    client.start_run = AsyncMock(
        return_value=DeployRunStart(run_id="qa-run-1", started=True, run_status=RunStatus.RUNNING)
    )
    client.create_task = AsyncMock()
    return client


async def _process(api, bot_message, redis, *, liveness_effect, sleeps=None):
    """Run one QA job with everything but the liveness probe held still.

    `sleeps` is an optional list the probe's waits are recorded into, for the
    cases where how long it waited is the behaviour under test.
    """
    api.get_bot_liveness = AsyncMock(**liveness_effect)
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("src.consumers.qa.api_client", api))
        enter(patch("src.consumers.qa.BOT_LIVENESS_RETRY_DELAY", 0))
        if sleeps is not None:

            async def _record(delay):
                sleeps.append(delay)

            enter(patch("src.consumers.qa.asyncio.sleep", _record))
        enter(
            patch(
                "src.consumers.qa.check_deployed_url_reachable",
                new_callable=AsyncMock,
                return_value=None,
            )
        )
        enter(
            patch(
                "src.consumers.qa._resolve_server_info",
                new_callable=AsyncMock,
                return_value=_server_info(),
            )
        )
        enter(
            patch(
                "src.consumers.qa.preflight_bot_access", new_callable=AsyncMock, return_value=None
            )
        )
        get_settings = enter(patch("src.consumers.qa.get_settings"))
        alert = enter(patch("src.consumers.qa.notify_admins_best_effort", new_callable=AsyncMock))
        run_centrally = enter(patch("src.consumers.qa.run_qa_centrally", new_callable=AsyncMock))
        get_settings.return_value = SimpleNamespace(
            qa_executor_agent_type=AgentType.CLAUDE,
            qa_capability_host="qa-worker",
            qa_llm_model=None,
            qa_llm_base_url=None,
            qa_llm_api_key=None,
        )
        run_centrally.return_value = QAResult(passed=True, summary="OK")
        result = await process_qa_job(bot_message, redis)
    return result, run_centrally, alert


class TestBotLivenessIsEstablishedBeforeTheExecutor:
    async def test_a_live_bot_is_established_and_the_token_never_enters_the_qa_runtime(
        self, api, bot_message, redis
    ):
        """The API asks Telegram with the token it holds; QA gets a state back."""
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={
                "return_value": BotLiveness(
                    state=BotLivenessState.ALIVE,
                    bot_username="weather_bot",
                    detail="getMe answered as @weather_bot",
                )
            },
        )

        assert result["status"] == "passed"
        api.get_bot_liveness.assert_awaited_once_with(bot_message["project_id"])
        facts = run_centrally.await_args[1]["established_facts"]
        assert any("answered getMe" in fact for fact in facts)
        # The path the answer came by is on the record, and no token is on it.
        assert any(
            f"projects/{bot_message['project_id']}/telegram/liveness" in fact for fact in facts
        )
        alert.assert_not_awaited()

    async def test_a_bot_telegram_refuses_blocks_the_run_with_no_executor_and_no_fix_task(
        self, api, bot_message, redis
    ):
        """A revoked token is deterministic, and no engineering worker can fix it."""
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={
                "return_value": BotLiveness(
                    state=BotLivenessState.NOT_LIVE,
                    detail="Telegram refused the stored token: HTTP 401, Unauthorized",
                )
            },
        )

        assert result["status"] == "qa_blocked"
        run_centrally.assert_not_awaited()
        api.create_task.assert_not_called()
        run_result = api.patch.await_args[1]["json"]["result"]
        assert run_result["qa_outcome"] == QAOutcome.BLOCKED.value
        assert run_result["blocker"]["category"] == QABlockerCategory.BOT_NOT_LIVE.value
        assert "telegram/liveness" in run_result["blocker"]["sent"]
        # Product-side, not infrastructure: no administrator is paged for it.
        alert.assert_not_awaited()

    async def test_telegram_unreachable_is_retried_then_reported_as_qa_infrastructure(
        self, api, bot_message, redis
    ):
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={
                "return_value": BotLiveness(
                    state=BotLivenessState.TELEGRAM_UNREACHABLE,
                    detail="getMe request failed: connection refused",
                )
            },
        )

        assert result["status"] == "qa_blocked"
        assert api.get_bot_liveness.await_count == BOT_LIVENESS_ATTEMPTS
        run_centrally.assert_not_awaited()
        run_result = api.patch.await_args[1]["json"]["result"]
        assert run_result["blocker"]["category"] == QABlockerCategory.QA_PROBE_UNAVAILABLE.value
        # The infrastructure half of DoD 5: an administrator is told, by the same
        # channel a missing executor uses.
        alert.assert_awaited_once()
        assert QABlockerCategory.QA_PROBE_UNAVAILABLE.value in alert.await_args[0][0]

    async def test_the_platform_api_not_answering_is_the_same_infrastructure_outcome(
        self, api, bot_message, redis
    ):
        """The question cannot be asked, which is not evidence about the bot."""
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={"side_effect": httpx.ConnectError("connection refused")},
        )

        assert result["status"] == "qa_blocked"
        assert api.get_bot_liveness.await_count == BOT_LIVENESS_ATTEMPTS
        run_centrally.assert_not_awaited()
        run_result = api.patch.await_args[1]["json"]["result"]
        assert run_result["blocker"]["category"] == QABlockerCategory.QA_PROBE_UNAVAILABLE.value
        alert.assert_awaited_once()

    async def test_being_rate_limited_is_infrastructure_and_waits_what_telegram_asked(
        self, api, bot_message, redis
    ):
        """Flood control is not a dead bot, and the wait is Telegram's number.

        HTTP 429 leaves the token untested: the API reports it as
        `TELEGRAM_UNREACHABLE` carrying `retry_after`, so the probe retries on
        that number and — when the window does not close — ends as the same QA
        infrastructure outcome a missing executor gets, never as `bot_not_live`.
        """
        sleeps: list[int] = []
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={
                "return_value": BotLiveness(
                    state=BotLivenessState.TELEGRAM_UNREACHABLE,
                    retry_after=7,
                    detail="getMe returned HTTP 429: Too Many Requests: retry after 7",
                )
            },
            sleeps=sleeps,
        )

        assert api.get_bot_liveness.await_count == BOT_LIVENESS_ATTEMPTS
        assert sleeps == [7] * (BOT_LIVENESS_ATTEMPTS - 1)
        assert result["status"] == "qa_blocked"
        run_centrally.assert_not_awaited()
        run_result = api.patch.await_args[1]["json"]["result"]
        assert run_result["blocker"]["category"] == QABlockerCategory.QA_PROBE_UNAVAILABLE.value
        alert.assert_awaited_once()

    async def test_a_flood_window_longer_than_the_probe_waits_stops_instead_of_sitting_on_it(
        self, api, bot_message, redis
    ):
        """Bounded means bounded: the budget is not spent waiting out an hour."""
        sleeps: list[int] = []
        result, run_centrally, alert = await _process(
            api,
            bot_message,
            redis,
            liveness_effect={
                "return_value": BotLiveness(
                    state=BotLivenessState.TELEGRAM_UNREACHABLE,
                    retry_after=BOT_LIVENESS_MAX_RETRY_DELAY + 1,
                    detail="getMe returned HTTP 429: Too Many Requests",
                )
            },
            sleeps=sleeps,
        )

        assert api.get_bot_liveness.await_count == 1
        assert sleeps == []
        assert result["status"] == "qa_blocked"
        run_centrally.assert_not_awaited()
        run_result = api.patch.await_args[1]["json"]["result"]
        assert run_result["blocker"]["category"] == QABlockerCategory.QA_PROBE_UNAVAILABLE.value
        assert str(BOT_LIVENESS_MAX_RETRY_DELAY + 1) in run_result["blocker"]["received"]
        alert.assert_awaited_once()

    async def test_a_deployment_without_a_bot_asks_nothing(self, api, bot_message, redis):
        """No bot, no question: the probe is bounded by what the deployment has."""
        bot_message["bot_username"] = None

        result, run_centrally, _ = await _process(
            api, bot_message, redis, liveness_effect={"return_value": None}
        )

        assert result["status"] == "passed"
        api.get_bot_liveness.assert_not_awaited()
        assert run_centrally.await_args[1]["established_facts"] == []
