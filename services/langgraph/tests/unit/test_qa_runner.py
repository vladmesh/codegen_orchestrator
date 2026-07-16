"""Unit tests for QA runner — HTTP health checks, SSH to server, parse result."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from shared.contracts.acceptance import HealthCriterion
from src.consumers._qa_runner import parse_qa_result, run_health_checks, run_qa_on_server
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

    def test_prompt_with_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )
        assert "@weather_bot" in prompt
        assert "Telegram" in prompt or "telethon" in prompt.lower()

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
            ' "detail": "200 OK"}], "summary": "All good"}'
        )
        result = parse_qa_result(raw)
        assert result.passed is True
        assert len(result.checks) == 1
        assert result.summary == "All good"

    def test_valid_fail_result(self):
        raw = (
            '{"pass": false, "checks": [{"name": "weather", "pass": false,'
            ' "detail": "404"}], "summary": "Broken"}'
        )
        result = parse_qa_result(raw)
        assert result.passed is False
        assert result.checks[0]["pass"] is False

    def test_malformed_json(self):
        result = parse_qa_result("not json at all")
        assert result.passed is False
        assert "parse" in result.summary.lower() or "failed" in result.summary.lower()

    def test_json_embedded_in_text(self):
        """Claude sometimes wraps JSON in markdown code blocks."""
        raw = """Here are the results:
```json
{"pass": true, "checks": [], "summary": "OK"}
```
"""
        result = parse_qa_result(raw)
        assert result.passed is True

    def test_missing_pass_field(self):
        raw = '{"checks": [], "summary": "test"}'
        result = parse_qa_result(raw)
        assert result.passed is False

    def test_empty_output(self):
        result = parse_qa_result("")
        assert result.passed is False

    def test_output_format_json_wrapper(self):
        """Claude Code --output-format json wraps result in envelope."""
        import json

        inner = json.dumps(
            {
                "pass": True,
                "checks": [{"name": "health", "pass": True, "detail": "200"}],
                "summary": "OK",
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
        import json

        wrapper = json.dumps(
            {"type": "result", "subtype": "success", "result": "No output produced"}
        )
        result = parse_qa_result(wrapper)
        assert result.passed is False
        assert "parse" in result.summary.lower() or "failed" in result.summary.lower()


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


class TestRunQAOnServer:
    @pytest.fixture(autouse=True)
    def _skip_credential_refresh(self):
        with patch("src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock):
            yield

    @pytest.mark.asyncio
    async def test_successful_qa_pass(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"pass": true, "checks": [], "summary": "All tests passed"}'
        mock_result.stderr = ""
        mock_result.exit_status = 0

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=mock_result)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        with patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh:
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.return_value = mock_conn

            result = await run_qa_on_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
                project_name="weather_bot",
                acceptance_criteria="Build a weather bot",
                deployed_url="https://weather.example.com",
            )

        assert result.passed is True
        assert mock_asyncssh.connect.call_args.kwargs["username"] == "dev"

    @pytest.mark.asyncio
    async def test_ssh_connection_failure(self):
        with patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh:
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.side_effect = OSError("Connection refused")

            result = await run_qa_on_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="fake",
                project_name="test",
                acceptance_criteria="Test",
                deployed_url="https://test.com",
            )

        assert result.passed is False
        assert "SSH" in result.summary or "connection" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_claude_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "Error: timeout exceeded"
        mock_result.exit_status = 1

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=mock_result)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        with patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh:
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.return_value = mock_conn

            result = await run_qa_on_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="fake",
                project_name="test",
                acceptance_criteria="Test",
                deployed_url="https://test.com",
            )

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_custom_timeout(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"pass": true, "checks": [], "summary": "OK"}'
        mock_result.stderr = ""
        mock_result.exit_status = 0

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=mock_result)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        with patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh:
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.return_value = mock_conn

            await run_qa_on_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="fake",
                project_name="test",
                acceptance_criteria="Test",
                deployed_url="https://test.com",
                timeout=600,
            )

        # Verify timeout is passed to the claude command (first run call)
        first_call = mock_conn.run.call_args_list[0]
        cmd = first_call[0][0]
        assert "600" in cmd
