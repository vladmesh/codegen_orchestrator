"""Validate the service fixture payload without running its service boundaries."""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import respx

from shared.contracts.acceptance import parse_health_only_criteria
from shared.contracts.dto.executor_diagnostics import (
    EXECUTOR_DIAGNOSTICS_REDIS_KEY,
    ExecutorAvailability,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.vocab import AgentType
from src.agents.qa.caller_identity import resolve_qa_caller_identity
from src.consumers import _qa_runner
from src.consumers._qa_redaction import QARunRedaction


@pytest.mark.asyncio
async def test_health_qa_service_snapshot_round_trips_without_model_sessions(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "service/test_health_qa_admission.py"
    spec = importlib.util.spec_from_file_location("health_qa_langgraph_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TEST_API_BASE_URL", "http://fixture.invalid")
    monkeypatch.setattr(module, "LanggraphAPIClient", Mock())
    monkeypatch.setattr(module, "RedisStreamClient", Mock(return_value=AsyncMock()))

    class SnapshotPublished(Exception):
        pass

    # Stop at the fixture's first write, before any service setup or state transition.
    redis = AsyncMock()
    redis.get.return_value = None
    redis.set.side_effect = SnapshotPublished
    fixture = module.health_qa.__wrapped__(redis)
    with pytest.raises(SnapshotPublished):
        await anext(fixture)
    key, payload = redis.set.call_args.args
    assert key == EXECUTOR_DIAGNOSTICS_REDIS_KEY
    snapshot = ExecutorDiagnosticSnapshot.model_validate_json(payload)
    assert {item.executor for item in snapshot.diagnostics} == {AgentType.CLAUDE, AgentType.CODEX}
    assert all(
        item.availability == ExecutorAvailability.UNAVAILABLE
        and item.reason_code == "profile_logged_out"
        for item in snapshot.diagnostics
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [200, 503, "refused"])
async def test_product_fixture_proves_identity_before_health_read(monkeypatch, response):
    path = Path(__file__).resolve().parents[1] / "service/test_health_qa_admission.py"
    spec = importlib.util.spec_from_file_location("health_qa_http_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(_qa_runner, "HEALTH_CHECK_RETRY_DELAY", 0)
    with respx.mock(assert_all_called=True) as transport:
        grant, read = module.product_routes(transport, response)
        assert await _qa_runner.check_deployed_url_reachable(module.PRODUCT) is None
        identity, blocker = await resolve_qa_caller_identity(
            deployed_url=module.PRODUCT, secrets=module.CAPABILITIES, telegram_account_id=None
        )
        assert blocker is None
        assert identity.user_ref == "qa:central-qa"
        assert grant.called
        result = await _qa_runner.run_health_checks(
            deployed_url=module.PRODUCT,
            checks=parse_health_only_criteria("- GET /health returns 200"),
            caller_identity=identity,
            redaction=QARunRedaction.from_stored(module.CAPABILITIES),
        )
        assert read.called
        assert result.passed == (response == 200)
        assert all(value not in result.report for value in module.CAPABILITIES.values())
        if response == "refused":
            assert result.blocker.category.value == "deployed_url_unreachable"
        else:
            assert result.blocker is None
        if response == 503:
            assert "got 503, expected 200" in result.report and "[redacted:" in result.report
