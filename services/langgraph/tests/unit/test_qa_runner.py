"""Unit tests for QA runner — HTTP health checks, the central agent, parse result."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx

from shared.contracts.acceptance import HealthCriterion
from shared.contracts.dto.run_result import QABlockerCategory
from src.consumers._qa_runner import parse_qa_result, run_health_checks
from src.prompts.qa import build_qa_prompt


class TestBuildQAPrompt:
    def test_basic_prompt(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- GET /health returns 200\n- GET /api/weather returns forecast",
            deployed_url="https://weather.example.com",
        )
        assert "GET /health returns 200" in prompt
        assert "https://weather.example.com" in prompt
        assert "regression" in prompt.lower()

    def test_prompt_requires_deterministic_identity_and_read_only_api(self):
        prompt = build_qa_prompt("- test stateful flow", "https://weather.example.com")

        assert "telegram_id=8202532144" in prompt
        assert "cannot write to the application" in prompt

    def test_prompt_never_offers_a_shell_or_a_second_target(self):
        """The rules must match the tools: no shell, one deployment."""
        prompt = build_qa_prompt("- GET /health returns 200", "https://api.example.com")

        assert "No shell" in prompt
        assert "exactly one deployment" in prompt
        # Nothing from the on-target runtime survives in the prompt.
        assert "claude" not in prompt.lower()
        assert "/opt/qa-runner" not in prompt
        assert ".qa-telethon.env" not in prompt

    def test_prompt_with_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )
        assert "@weather_bot" in prompt
        assert "telegram_probe" in prompt

    def test_bot_prompt_never_hands_the_agent_telegram_credentials(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )

        # The session used to be exported into the agent's shell. It is now held
        # by the runtime and reachable only through one tool.
        assert "TELETHON_SESSION" not in prompt
        assert "StringSession" not in prompt
        assert "never hold the account's credentials" in prompt

    def test_bot_prompt_forbids_reporting_telegram_checks_as_blocked(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )

        assert '"Blocked", "skipped" and "cannot test" are not allowed results' in prompt

    def test_prompt_without_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- GET /api/items returns list",
            deployed_url="https://api.example.com",
        )
        assert "@" not in prompt


class TestParseQAResult:
    def test_valid_pass_result(self):
        raw = (
            '{"pass": true, "checks": [{"name": "health", "pass": true,'
            ' "detail": "200 OK"}], "summary": "All good", "state_changes": []}'
        )
        result = parse_qa_result(raw)
        assert result.passed is True
        assert len(result.checks) == 1
        assert result.summary == "All good"

    def test_agent_state_changes_are_not_trusted_as_cleanup_evidence(self):
        result = parse_qa_result(
            '{"pass": true, "checks": [], "summary": "OK", '
            '"state_changes": [{"resource": "user telegram_id=8202532144", '
            '"operation": "created", "cleanup": {"attempted": true, '
            '"succeeded": true, "detail": "DELETE returned 204"}}]}'
        )

        assert result.state_changes == []

    def test_agent_claim_of_failed_cleanup_does_not_override_runner_verdict(self):
        result = parse_qa_result(
            '{"pass": true, "checks": [], "summary": "OK", '
            '"state_changes": [{"resource": "user telegram_id=8202532144", '
            '"operation": "created", "cleanup": {"attempted": true, '
            '"succeeded": false, "detail": "DELETE returned 405"}}]}'
        )

        assert result.passed is True
        assert result.blocker is None

    def test_state_changes_are_not_required_from_agent(self):
        result = parse_qa_result('{"pass": true, "checks": [], "summary": "OK"}')

        assert result.passed is True
        assert result.blocker is None

    def test_valid_fail_result(self):
        raw = (
            '{"pass": false, "checks": [{"name": "weather", "pass": false,'
            ' "detail": "404"}], "summary": "Broken", "state_changes": []}'
        )
        result = parse_qa_result(raw)
        assert result.passed is False
        assert result.checks[0]["pass"] is False

    def test_malformed_json(self):
        result = parse_qa_result("not json at all")
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    def test_json_embedded_in_text(self):
        """Claude sometimes wraps JSON in markdown code blocks."""
        raw = """Here are the results:
```json
{"pass": true, "checks": [], "summary": "OK", "state_changes": []}
```
"""
        result = parse_qa_result(raw)
        assert result.passed is True

    def test_missing_pass_field(self):
        raw = '{"checks": [], "summary": "test"}'
        result = parse_qa_result(raw)
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    @pytest.mark.parametrize(
        "raw",
        [
            '{"pass": false, "checks": [42], "summary": "bad"}',
            '{"pass": true, "checks": "claimed all good", "summary": "bad"}',
        ],
    )
    def test_structurally_invalid_result_is_unknown_blocker(self, raw):
        result = parse_qa_result(raw)

        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    def test_empty_output(self):
        result = parse_qa_result("")
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    def test_output_format_json_wrapper(self):
        """Claude Code --output-format json wraps result in envelope."""
        inner = json.dumps(
            {
                "pass": True,
                "checks": [{"name": "health", "pass": True, "detail": "200"}],
                "summary": "OK",
                "state_changes": [],
            }
        )
        wrapper = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": inner}
        )
        result = parse_qa_result(wrapper)
        assert result.passed is True
        assert len(result.checks) == 1

    def test_output_format_json_wrapper_non_json_result(self):
        """When Claude Code returns non-JSON text in result field."""
        wrapper = json.dumps(
            {"type": "result", "subtype": "success", "result": "No output produced"}
        )
        result = parse_qa_result(wrapper)
        assert result.passed is False
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN


class TestRunHealthChecks:
    """GET criteria are decided against the deployed URL — no SSH, no LLM."""

    @pytest.fixture(autouse=True)
    def _no_retry_delay(self):
        """Keep the retry loop's timing out of the test's wall clock."""
        with patch("src.consumers._qa_runner.HEALTH_CHECK_RETRY_DELAY", 0):
            yield

    @respx.mock
    @pytest.mark.asyncio
    async def test_http_200_passes(self):
        """The mega health-only case: service answers 200 → QA passes."""
        route = respx.get("http://svc.example.com/health").mock(return_value=httpx.Response(200))

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is True
        assert route.called
        assert result.checks == [
            {"name": "GET /health returns 200", "pass": True, "detail": "got 200"}
        ]
        assert "http://svc.example.com" in result.summary

    @respx.mock
    @pytest.mark.asyncio
    async def test_trailing_slash_does_not_double_up(self):
        respx.get("http://svc.example.com/health").mock(return_value=httpx.Response(200))

        result = await run_health_checks(
            deployed_url="http://svc.example.com/",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_redirect_criterion_checks_the_redirect_itself(self):
        """ "returns 301" means the path answers 301, not that it leads somewhere 200."""
        redirect = respx.get("http://svc.example.com/old").mock(
            return_value=httpx.Response(301, headers={"Location": "http://svc.example.com/new"})
        )
        destination = respx.get("http://svc.example.com/new").mock(return_value=httpx.Response(200))

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/old", expected_status=301)],
        )

        assert result.passed is True
        assert redirect.called
        # Following the redirect would report the destination's 200 and fail a
        # criterion the service actually satisfies.
        assert not destination.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_redirected_path_does_not_pass_a_200_criterion(self):
        """The inverse: a 301 must not be laundered into the 200 the criterion wants."""
        respx.get("http://svc.example.com/health").mock(
            return_value=httpx.Response(301, headers={"Location": "http://svc.example.com/ok"})
        )
        respx.get("http://svc.example.com/ok").mock(return_value=httpx.Response(200))

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is False
        assert result.checks[0]["detail"] == "got 301, expected 200"

    @respx.mock
    @pytest.mark.asyncio
    async def test_wrong_status_fails_with_detail(self):
        respx.get("http://svc.example.com/health").mock(return_value=httpx.Response(502))

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is False
        assert result.checks[0]["pass"] is False
        assert result.checks[0]["detail"] == "got 502, expected 200"

    @respx.mock
    @pytest.mark.asyncio
    async def test_retries_while_the_service_comes_up(self):
        """A service still starting must not fail the run on the first 503."""
        route = respx.get("http://svc.example.com/health").mock(
            side_effect=[
                httpx.Response(503),
                httpx.ConnectError("connection refused"),
                httpx.Response(200),
            ]
        )

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is True
        assert route.call_count == 3

    @respx.mock
    @pytest.mark.asyncio
    async def test_unreachable_service_fails_after_attempts(self):
        from src.consumers._qa_runner import HEALTH_CHECK_ATTEMPTS

        route = respx.get("http://svc.example.com/health").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[HealthCriterion(path="/health", expected_status=200)],
        )

        assert result.passed is False
        assert route.call_count == HEALTH_CHECK_ATTEMPTS
        assert "request failed" in result.checks[0]["detail"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_one_failing_check_fails_the_run(self):
        respx.get("http://svc.example.com/health").mock(return_value=httpx.Response(200))
        respx.get("http://svc.example.com/ready").mock(return_value=httpx.Response(404))

        result = await run_health_checks(
            deployed_url="http://svc.example.com",
            checks=[
                HealthCriterion(path="/health", expected_status=200),
                HealthCriterion(path="/ready", expected_status=200),
            ],
        )

        assert result.passed is False
        assert [c["pass"] for c in result.checks] == [True, False]
        assert "1/2" in result.summary
