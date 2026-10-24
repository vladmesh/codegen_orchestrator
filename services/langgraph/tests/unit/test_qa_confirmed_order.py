"""QA of a story with a confirmed order: the queued message in, the settled Run out.

The message is the typed `QAMessage` the deploy handoff publishes for an
INSTALL-only plan: scaffold health criteria beside the confirmed brief's order
(`shared/tests/confirmed_order_cases.py`). The runner is stood in for by the
runner's own verdict settlement over a scripted executor verdict, so what the
Run records is what the runner decides.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.qa_verification import QAVerificationGapsRecorded
from shared.contracts.dto.run import RunType
from shared.contracts.dto.server import ServerDTO
from shared.contracts.queues.qa import QAConfirmedOrder, QAMessage, QAOutcome
from shared.contracts.vocab import AgentType
from shared.qa_identity import QA_SSH_USER, QA_SSH_USER_LABEL
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.tests.confirmed_order_cases import SCAFFOLD_CRITERIA, channels_brief
from src.consumers._qa_runner import (
    QAResult,
    parse_qa_result,
    require_confirmed_order,
    settle_unverified_checks,
)
from src.consumers.qa import process_qa_job

PROJECT_ID = "00000000-0000-0000-0000-000000000001"
HEALTH = {"name": "GET /health returns 200", "pass": True, "detail": "200"}


def _message(*, confirmed_order: QAConfirmedOrder | None) -> dict:
    """The handoff's typed message, as the queue carries it."""
    return QAMessage(
        story_id="story-1",
        project_id=PROJECT_ID,
        initiating_run_id="live-1",
        deployed_url="https://channels.example.com",
        application_id=1,
        acceptance_criteria=SCAFFOLD_CRITERIA,
        confirmed_order=confirmed_order,
        run_id="qa-run-1",
    ).model_dump(mode="json")


ORDER = QAConfirmedOrder.from_brief(channels_brief())


@pytest.fixture
def api():
    now = datetime.now(UTC)
    with patch("src.consumers.qa.api_client") as client:
        client.get_project = AsyncMock(
            return_value=ProjectDTO(
                id=PROJECT_ID,
                initiating_run_id="live-1",
                title="channels",
                slug="channels-0000",
                status=ProjectStatus.ACTIVE,
                config={},
                owner_id=1,
                created_at=now,
                updated_at=now,
            )
        )
        client.get_product_brief_by_story = AsyncMock(return_value=channels_brief())
        client.get_application = AsyncMock(
            return_value=SimpleNamespace(server_handle="vps-1", ports=[])
        )
        client.get_server = AsyncMock(
            return_value=ServerDTO(
                handle="vps-1",
                host="vps-1.example.com",
                public_ip="1.2.3.4",
                ssh_user="dev",
                status="active",
                is_managed=True,
                labels={QA_SSH_USER_LABEL: QA_SSH_USER},
                qa_target_version=QA_TARGET_PROFILE_VERSION,
                qa_target_proved_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        client.get_server_ssh_key = AsyncMock(return_value="fleet-key")
        client.start_run = AsyncMock(return_value=SimpleNamespace(started=True))
        client.get_run = AsyncMock(
            return_value=SimpleNamespace(
                run_metadata=ExecutorDecision(
                    attempt_kind=RunType.QA,
                    agent_type=AgentType.CLAUDE,
                    source=ExecutorDecisionSource.QA_API_SETTING,
                    policy_version="v1",
                    reason="fixture",
                ).as_run_metadata()
            )
        )
        client.list_qa_probes = AsyncMock(return_value=[])
        client.patch = AsyncMock(return_value={})
        client.record_verification_gaps_from_run = AsyncMock(
            return_value=QAVerificationGapsRecorded(recorded=["x"])
        )
        yield client


@pytest.fixture(autouse=True)
def _runtime():
    settings = SimpleNamespace(qa_executor_agent_type=AgentType.CLAUDE, qa_capability_host="qa")
    with (
        patch("src.consumers.qa.get_settings", return_value=settings),
        patch("src.consumers.qa.check_deployed_url_reachable", AsyncMock(return_value=None)),
    ):
        yield


def _executor_verdict(*checks: dict):
    """`run_qa_centrally` settling a scripted executor verdict the way the runner does."""

    async def run(**kwargs):
        failed = any(check["pass"] is False for check in checks)
        raw = json.dumps({"pass": not failed, "checks": list(checks), "summary": "ran"})
        return require_confirmed_order(
            settle_unverified_checks(parse_qa_result(raw)), kwargs["order"]
        )

    return AsyncMock(side_effect=run)


async def _process(message: dict, runner: AsyncMock):
    redis = SimpleNamespace(redis=AsyncMock())
    redis.redis.set = AsyncMock(return_value=True)
    health = AsyncMock(return_value=QAResult(passed=True, checks=[HEALTH], summary="ok"))
    with (
        patch("src.consumers.qa.run_qa_centrally", runner),
        patch("src.consumers.qa.run_health_checks", health),
    ):
        outcome = await process_qa_job(message, redis)
    return outcome, health


def _settled(api) -> dict:
    return api.patch.call_args.kwargs["json"]["result"]


ORDER_NAMES = [f"[{row.id}] {row.text}" for row in ORDER.must_requirements]


async def test_scaffold_health_alone_leaves_the_order_unverified_and_out_of_repair(api):
    runner = _executor_verdict(HEALTH)

    outcome, health = await _process(_message(confirmed_order=ORDER), runner)

    health.assert_not_awaited()
    handed = runner.await_args.kwargs
    assert handed["order"] == ORDER
    assert handed["acceptance_criteria"] == SCAFFOLD_CRITERIA
    assert outcome["status"] == "qa_blocked"
    result = _settled(api)
    assert result["qa_outcome"] == QAOutcome.BLOCKED.value
    assert result["blocker"]["category"] == "order_unverified"
    assert result["failed_checks"] == []
    assert result["passed_checks"] == ["GET /health returns 200"]
    assert [check["name"] for check in result["unverified_checks"]] == ORDER_NAMES
    assert all(check["reason"] for check in result["unverified_checks"])
    api.record_verification_gaps_from_run.assert_awaited_once_with(PROJECT_ID, "qa-run-1")


async def test_a_passing_brief_interaction_passes_with_the_rest_as_gaps(api):
    runner = _executor_verdict(
        HEALTH, {"name": "[r1] /channels answers", "pass": True, "detail": "Список каналов"}
    )

    outcome, _ = await _process(_message(confirmed_order=ORDER), runner)

    assert outcome["status"] == "passed"
    result = _settled(api)
    assert result["qa_outcome"] == QAOutcome.PASSED.value
    assert result["passed_checks"] == ["GET /health returns 200", "[r1] /channels answers"]
    assert [check["name"] for check in result["unverified_checks"]] == ORDER_NAMES[1:]
    api.record_verification_gaps_from_run.assert_awaited_once_with(PROJECT_ID, "qa-run-1")


async def test_a_failing_brief_interaction_is_a_product_failure(api):
    failure = {
        "name": "[r2] /addchannel answers",
        "pass": False,
        "detail": "/addchannel https://t.me/example produced no reply",
        "cause": "product",
    }

    outcome, _ = await _process(_message(confirmed_order=ORDER), _executor_verdict(HEALTH, failure))

    assert outcome["status"] == "qa_failed"
    result = _settled(api)
    assert result["qa_outcome"] == QAOutcome.FAILED.value
    assert result["failed_checks"] == [{key: failure[key] for key in ("name", "detail", "cause")}]
    assert result["blocker"] is None


async def test_without_a_confirmed_order_health_criteria_stay_executor_free(api):
    runner = _executor_verdict(HEALTH)

    outcome, health = await _process(_message(confirmed_order=None), runner)

    runner.assert_not_awaited()
    health.assert_awaited_once()
    assert outcome["status"] == "passed"
    assert _settled(api)["qa_outcome"] == QAOutcome.PASSED.value


async def test_a_fixed_stand_probe_still_decides_a_confirmed_order_without_an_executor(api):
    message = _message(confirmed_order=ORDER)
    message["acceptance_criteria"] += "\n- Stand mechanical reminders: e2e-0f1e2d3c4b5a"
    runner = _executor_verdict(HEALTH)

    _, health = await _process(message, runner)

    runner.assert_not_awaited()
    health.assert_awaited_once()
    # The probe ran and settled the Run itself; it needs a bot this fixture lacks.
    assert [check["name"] for check in _settled(api)["failed_checks"]] == [
        "mechanical Telegram probe"
    ]
