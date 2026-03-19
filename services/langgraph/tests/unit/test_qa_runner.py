"""Unit tests for QA runner — SSH to server, run Claude Code, parse result."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.consumers._qa_runner import (
    build_qa_prompt,
    parse_qa_result,
    run_qa_on_server,
)


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


class TestRunQAOnServer:
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
                ssh_key="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
                project_name="weather_bot",
                acceptance_criteria="Build a weather bot",
                deployed_url="https://weather.example.com",
            )

        assert result.passed is True
        assert mock_asyncssh.connect.called

    @pytest.mark.asyncio
    async def test_ssh_connection_failure(self):
        with patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh:
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.side_effect = OSError("Connection refused")

            result = await run_qa_on_server(
                server_ip="1.2.3.4",
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
