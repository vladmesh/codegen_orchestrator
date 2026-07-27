"""Unit tests for QA runner — HTTP health checks, SSH to server, parse result."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from shared.contracts.acceptance import HealthCriterion
from shared.contracts.dto.run_result import QABlockerCategory
from src.consumers._qa_runner import (
    TelethonCredentialsError,
    _preflight_agent_qa,
    _require_telethon_credentials,
    parse_qa_result,
    run_health_checks,
    run_qa_on_server,
    verify_telegram_access_revoked,
)
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
        assert "POST, PUT, PATCH, or DELETE" in prompt
        assert "never send" in prompt.lower()

    def test_prompt_with_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )
        assert "@weather_bot" in prompt
        assert "Telegram" in prompt or "telethon" in prompt.lower()

    def test_bot_prompt_connects_with_a_string_session_and_real_app_creds(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )

        # Telethon needs real api_id/api_hash even with an authorized session
        assert "StringSession(os.environ['TELETHON_SESSION'])" in prompt
        assert "int(os.environ['TELETHON_API_ID'])" in prompt
        assert "os.environ['TELETHON_API_HASH']" in prompt
        assert "api_id=0" not in prompt
        assert "/opt/qa-runner/telethon.session" not in prompt

    def test_bot_prompt_leaves_credential_loading_to_the_runner(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )

        # The runner sources the file into the command's environment; asking the
        # agent to do it is a step it can skip, and did (run qa-7729960c).
        assert ". $HOME/.qa-telethon.env" not in prompt
        assert "already exported" in prompt

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


class TestRunQAOnServer:
    @pytest.fixture(autouse=True)
    def _skip_credential_refresh(self):
        with (
            patch("src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock),
            patch(
                "src.consumers._qa_runner._preflight_agent_qa", new_callable=AsyncMock
            ) as preflight,
        ):
            preflight.return_value = None
            yield

    @pytest.fixture
    def _skip_telethon_check(self):
        with patch(
            "src.consumers._qa_runner._require_telethon_credentials", new_callable=AsyncMock
        ):
            yield

    @pytest.mark.asyncio
    async def test_successful_qa_pass(self):
        mock_result = MagicMock()
        mock_result.stdout = (
            '{"pass": true, "checks": [], "summary": "All tests passed", "state_changes": []}'
        )
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
                project_name="weather bot",
                acceptance_criteria="Build a weather bot",
                deployed_url="https://weather.example.com",
            )

        assert result.passed is True
        assert mock_asyncssh.connect.call_args.kwargs["username"] == "dev"
        qa_cmd = next(
            call.args[0] for call in mock_conn.run.await_args_list if "claude -p" in call.args[0]
        )
        assert "cd '/opt/services/weather bot'" in qa_cmd

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
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN
        assert "exit_status=1" in result.blocker.received
        assert "timeout exceeded" in result.blocker.received

    @pytest.mark.asyncio
    async def test_custom_timeout(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"pass": true, "checks": [], "summary": "OK", "state_changes": []}'
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

        cmd = next(
            call.args[0] for call in mock_conn.run.await_args_list if "claude -p" in call.args[0]
        )
        assert "600" in cmd

    @pytest.mark.asyncio
    async def test_bot_run_loads_telethon_env_before_claude(self, _skip_telethon_check):
        mock_result = MagicMock()
        mock_result.stdout = '{"pass": true, "checks": [], "summary": "OK", "state_changes": []}'
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
                project_name="weather_bot",
                acceptance_criteria="- Telegram: /start responds",
                deployed_url="https://bot.example.com",
                bot_username="weather_bot",
            )

        cmd = next(
            call.args[0] for call in mock_conn.run.await_args_list if "claude -p" in call.args[0]
        )
        # TELETHON_* must be in the environment claude inherits, not something
        # the agent has to remember to source
        assert cmd.index("set -a && . $HOME/.qa-telethon.env && set +a") < cmd.index("claude -p")

    @pytest.mark.asyncio
    async def test_run_without_bot_does_not_touch_telethon_env(self):
        mock_result = MagicMock()
        mock_result.stdout = '{"pass": true, "checks": [], "summary": "OK", "state_changes": []}'
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
                project_name="api",
                acceptance_criteria="- GET /health returns 200",
                deployed_url="https://api.example.com",
            )

        assert all(".qa-telethon.env" not in call.args[0] for call in mock_conn.run.await_args_list)


class TestVerifyTelegramAccessRevoked:
    @pytest.mark.asyncio
    async def test_parses_server_key_before_opening_revocation_probe(self):
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        denied = MagicMock(category=QABlockerCategory.TELEGRAM_ACCESS_DENIED)
        with (
            patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh,
            patch(
                "src.consumers._qa_runner._probe_telegram_bot_access", new_callable=AsyncMock
            ) as probe,
        ):
            mock_asyncssh.import_private_key.return_value = "parsed-key"
            mock_asyncssh.connect.return_value = conn
            probe.return_value = denied
            result = await verify_telegram_access_revoked(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="-----BEGIN PRIVATE KEY-----\\nfake\\n-----END PRIVATE KEY-----",
                bot_username="private_bot",
            )

        assert result is denied
        mock_asyncssh.import_private_key.assert_called_once()
        assert mock_asyncssh.connect.call_args.kwargs["client_keys"] == ["parsed-key"]


class TestQAPreflight:
    @pytest.mark.asyncio
    async def test_claude_lookup_uses_the_agent_runtime_path(self):
        claude_present = SimpleNamespace(
            exit_status=0, stdout="/home/dev/.local/bin/claude\n", stderr=""
        )
        conn = AsyncMock()
        conn.run = AsyncMock(return_value=claude_present)

        blocker = await _preflight_agent_qa(conn, None)

        assert blocker is None
        conn.run.assert_awaited_once_with(
            'export PATH="$HOME/.local/bin:$PATH" && command -v claude', check=False
        )

    @pytest.mark.asyncio
    async def test_missing_telethon_credentials_returns_blocker_before_agent(self):
        missing = TelethonCredentialsError("no credentials file at $HOME/.qa-telethon.env")
        claude_present = SimpleNamespace(exit_status=0, stdout="/usr/bin/claude\n", stderr="")
        conn = AsyncMock()
        conn.run = AsyncMock(return_value=claude_present)

        with patch(
            "src.consumers._qa_runner._require_telethon_credentials",
            new_callable=AsyncMock,
            side_effect=missing,
        ):
            blocker = await _preflight_agent_qa(conn, "private_bot")

        assert blocker is not None
        assert blocker.category.value == "missing_telethon_credentials"
        assert blocker.received == str(missing)
        conn.run.assert_awaited_once_with(
            'export PATH="$HOME/.local/bin:$PATH" && command -v claude', check=False
        )

    @pytest.mark.asyncio
    async def test_bot_access_denial_reply_blocks_before_claude_runs(self):
        claude_present = SimpleNamespace(
            exit_status=0, stdout="/home/dev/.local/bin/claude\n", stderr=""
        )
        access_denied = SimpleNamespace(
            exit_status=2,
            stdout=(
                "telegram_access_denied:\ud83d\udeab "
                "\u0414\u043e\u0441\u0442\u0443\u043f "
                "\u0437\u0430\u043f\u0440\u0435\u0449\u0451\u043d\n"
            ),
            stderr="",
        )
        conn = AsyncMock()
        conn.run = AsyncMock(side_effect=[claude_present, access_denied])

        with patch(
            "src.consumers._qa_runner._require_telethon_credentials", new_callable=AsyncMock
        ):
            blocker = await _preflight_agent_qa(conn, "private_bot")

        assert blocker is not None
        assert blocker.category.value == "telegram_access_denied"
        assert blocker.sent == "Telegram /start to @private_bot"
        assert (
            "\u0414\u043e\u0441\u0442\u0443\u043f \u0437\u0430\u043f\u0440\u0435\u0449\u0451\u043d"
            in blocker.received
        )
        probe = conn.run.await_args_list[1].args[0]
        assert "get_messages" in probe
        assert "telegram_access_denied" in probe

    @pytest.mark.asyncio
    async def test_access_denial_stdout_wins_over_probe_stderr(self):
        """The bot reply remains the verdict when Telethon also writes diagnostics."""
        claude_present = SimpleNamespace(
            exit_status=0, stdout="/home/dev/.local/bin/claude\n", stderr=""
        )
        access_denied = SimpleNamespace(
            exit_status=2,
            stdout="telegram_access_denied:🚫 Доступ запрещён\n",
            stderr="Telethon reconnect diagnostic",
        )
        conn = AsyncMock()
        conn.run = AsyncMock(side_effect=[claude_present, access_denied])

        with patch(
            "src.consumers._qa_runner._require_telethon_credentials", new_callable=AsyncMock
        ):
            blocker = await _preflight_agent_qa(conn, "private_bot")

        assert blocker is not None
        assert blocker.category.value == "telegram_access_denied"
        assert blocker.received == "🚫 Доступ запрещён"

    @pytest.mark.asyncio
    async def test_wrong_telethon_identity_blocks_before_claude_runs(self):
        claude_present = SimpleNamespace(
            exit_status=0, stdout="/home/dev/.local/bin/claude\n", stderr=""
        )
        wrong_identity = SimpleNamespace(
            exit_status=3,
            stdout="telegram_identity_mismatch:expected=8202532144;actual=999\n",
            stderr="",
        )
        conn = AsyncMock()
        conn.run = AsyncMock(side_effect=[claude_present, wrong_identity])

        with patch(
            "src.consumers._qa_runner._require_telethon_credentials", new_callable=AsyncMock
        ):
            blocker = await _preflight_agent_qa(conn, "private_bot")

        assert blocker is not None
        assert blocker.category is QABlockerCategory.UNKNOWN
        assert blocker.received == "expected=8202532144;actual=999"
        probe = conn.run.await_args_list[1].args[0]
        assert "client.get_me()" in probe
        assert "8202532144" in probe


class TestRunQAOnServerPreflight:
    @pytest.mark.asyncio
    async def test_access_denial_reply_skips_claude(self):
        claude_present = SimpleNamespace(
            exit_status=0, stdout="/home/dev/.local/bin/claude\n", stderr=""
        )
        access_denied = SimpleNamespace(
            exit_status=2,
            stdout=(
                "telegram_access_denied:\ud83d\udeab "
                "\u0414\u043e\u0441\u0442\u0443\u043f "
                "\u0437\u0430\u043f\u0440\u0435\u0449\u0451\u043d\n"
            ),
            stderr="",
        )
        conn = AsyncMock()
        conn.run = AsyncMock(side_effect=[claude_present, access_denied])
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.consumers._qa_runner.asyncssh") as mock_asyncssh,
            patch("src.consumers._qa_runner._require_telethon_credentials", new_callable=AsyncMock),
            patch(
                "src.consumers._qa_runner._ensure_claude_credentials", new_callable=AsyncMock
            ) as ensure_credentials,
        ):
            mock_asyncssh.import_private_key.return_value = "parsed_key"
            mock_asyncssh.connect.return_value = conn

            result = await run_qa_on_server(
                server_ip="1.2.3.4",
                ssh_user="dev",
                ssh_key="fake",
                project_name="private_bot",
                acceptance_criteria="- Telegram: /start responds",
                deployed_url="https://bot.example.com",
                bot_username="private_bot",
            )

        assert result.blocker is not None
        assert result.blocker.category.value == "telegram_access_denied"
        ensure_credentials.assert_not_awaited()
        assert all("claude -p" not in call.args[0] for call in conn.run.await_args_list)


class _LocalShellConn:
    """Runs the checked command in a real bash with HOME pointed at a tmpdir."""

    def __init__(self, home):
        self.home = str(home)

    async def run(self, command, check=False):
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            env={"HOME": self.home, "PATH": os.environ["PATH"]},
        )
        return SimpleNamespace(stdout=proc.stdout, stderr=proc.stderr, exit_status=proc.returncode)


class TestRequireTelethonCredentials:
    """The check runs as shell on the server, so run it as shell here too."""

    def _write_env(self, home, body):
        (home / ".qa-telethon.env").write_text(body)

    @pytest.mark.asyncio
    async def test_complete_credentials_pass(self, tmp_path):
        self._write_env(
            tmp_path,
            "TELETHON_API_ID=123\nTELETHON_API_HASH=abc\nTELETHON_SESSION=1BVtsOK...\n",
        )

        await _require_telethon_credentials(_LocalShellConn(tmp_path))

    @pytest.mark.asyncio
    async def test_missing_file_raises(self, tmp_path):
        with pytest.raises(TelethonCredentialsError) as excinfo:
            await _require_telethon_credentials(_LocalShellConn(tmp_path))

        assert "no credentials file" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_empty_variable_is_named_in_the_error(self, tmp_path):
        self._write_env(
            tmp_path,
            "TELETHON_API_ID=123\nTELETHON_API_HASH=abc\nTELETHON_SESSION=\n",
        )

        with pytest.raises(TelethonCredentialsError) as excinfo:
            await _require_telethon_credentials(_LocalShellConn(tmp_path))

        assert "TELETHON_SESSION" in str(excinfo.value)
        assert "TELETHON_API_ID" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_error_never_carries_credential_values(self, tmp_path):
        self._write_env(
            tmp_path,
            "TELETHON_API_ID=123\nTELETHON_API_HASH=s3cr3thash\nTELETHON_SESSION=\n",
        )

        with pytest.raises(TelethonCredentialsError) as excinfo:
            await _require_telethon_credentials(_LocalShellConn(tmp_path))

        assert "s3cr3thash" not in str(excinfo.value)
