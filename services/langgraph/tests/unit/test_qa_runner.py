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
        """The rules must match the sandbox: the target URL and Telegram, one deployment."""
        prompt = build_qa_prompt("- GET /health returns 200", "https://api.example.com")
        flat = " ".join(prompt.split())

        assert "You have a shell in a sandbox" in prompt
        assert "a script you write reaches exactly two places" in flat
        assert "Nothing else — the fleet, the internet, another port" in flat
        assert "exactly one deployment" in prompt
        # Nothing from the on-target runtime survives in the prompt.
        assert "claude" not in prompt.lower()
        assert "/opt/qa-runner" not in prompt
        assert ".qa-telethon.env" not in prompt

    def test_failed_check_detail_quotes_expected_and_received(self):
        """A QA-fix worker is told the exact wording QA wanted, not only that it was wrong."""
        prompt = build_qa_prompt(
            acceptance_criteria="- /income 5000 зарплата → «Доход 5000 «зарплата» записан.»",
            deployed_url="https://bot.example.com",
        )

        assert "## What a failed check's detail says" in prompt
        assert (
            "`expected: <value or wording quoted from the criterion>; "
            "received: <actual value or reply>`" in prompt
        )
        assert "without both quotes, is\nnot a valid failed check" in prompt

    def test_accumulated_state_is_judged_as_a_change_from_the_observed_start(self):
        """QA's earlier records stay in the product, so a balance is judged as a change."""
        prompt = build_qa_prompt(
            acceptance_criteria="- после дохода 5000 и расхода 300 /balance отвечает 4700",
            deployed_url="https://bot.example.com",
        )
        flat = " ".join(prompt.split())

        assert "## Accumulated state" in prompt
        assert 'a balance, a total, a count, a list of records, "no records yet"' in flat
        assert "First read the starting value through the same observable" in flat
        assert "Perform the criterion's sequence." in flat
        assert "is met when the balance grew by 4700 from the observed start" in flat
        assert "the reply keeps the criterion's wording form" in flat
        assert "names the observed starting value" in flat
        assert "`expected: /balance отвечает 14100 (start 9400 + 4700); received: 9400`" in flat
        assert "A starting value you cannot read makes the check unverifiable" in flat
        assert "cause `qa_capability`" in flat and "`qa_access`" in flat
        assert "is still matched exactly as the criterion words it" in flat
        # The write prohibition and the one identity are unchanged.
        assert "You cannot write to the application's data, and must not try." in prompt
        assert "telegram_id=8202532144" in prompt

    def test_prompt_with_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )
        assert "@weather_bot" in prompt
        assert "telegram_probe" in prompt
        assert "post-press evidence" in prompt

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
        # A probe of its own gets the account only as the file the CLI writes.
        assert "`qa telegram_identity` writes for your Telethon client; never print that file" in (
            " ".join(prompt.split())
        )

    def test_bot_prompt_forbids_reporting_telegram_checks_as_blocked(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- Telegram: /start responds with welcome",
            deployed_url="https://bot.example.com",
            bot_username="weather_bot",
        )

        assert '"Blocked", "skipped" and "cannot test" are not allowed results' in prompt

    def test_prompt_asks_for_a_cause_on_every_failed_check(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- POST /api/transactions creates a transaction",
            deployed_url="https://api.example.com",
            bot_username="weather_bot",
        )

        assert '"cause": "product" | "qa_capability" | "qa_access"' in prompt
        assert "Every failed check carries a `cause`" in prompt
        flat = " ".join(prompt.split())
        assert '`qa_capability` — the criterion needs an action "What you can check" does' in flat
        assert "photo upload" not in prompt
        assert "fails with cause `qa_capability`" in flat
        assert "fails with cause `qa_access`" in prompt
        assert prompt.count("it is never a product failure") == 2

    def test_prompt_without_bot_username(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- GET /api/items returns list",
            deployed_url="https://api.example.com",
        )
        assert "@" not in prompt


class TestEstablishedFactsKeepTheContract:
    """Facts the runner established are stated, and nothing else changes.

    The executor is told what is already known so it does not spend the run
    asking again. What it may call, what it must not do, and the JSON it has to
    return are the same prompt either way — that is the contract, and this is
    where it is checked rather than asserted in prose.
    """

    FACT = "- Container state, read from the target with docker inspect: web — running."

    def _prompts(self) -> tuple[str, str]:
        plain = build_qa_prompt("- GET /health returns 200", "https://api.example.com")
        with_facts = build_qa_prompt(
            "- GET /health returns 200",
            "https://api.example.com",
            established_facts=[self.FACT],
        )
        return plain, with_facts

    def test_the_established_fact_replaces_the_checklist_item_it_answers(self):
        plain, with_facts = self._prompts()

        assert self.FACT in with_facts
        assert "Already established (checked by the QA runner, not by you)" in with_facts
        assert "3. Container state — already established above; do not check it again" in with_facts
        assert "3. Containers running and healthy (no restart loops)" in plain

    def test_nothing_else_of_the_prompt_changes(self):
        """The only line that leaves the prompt is the one now answered."""
        plain, with_facts = self._prompts()

        dropped = [line for line in plain.splitlines() if line not in with_facts.splitlines()]

        assert dropped == ["3. Containers running and healthy (no restart loops)"]

    def test_the_result_contract_is_the_same_either_way(self):
        plain, with_facts = self._prompts()

        for prompt in (plain, with_facts):
            assert '"pass": true/false' in prompt
            assert '{"name": "passed check", "pass": true, "detail": "one-line summary"}' in prompt
            assert '{"name": "failed check", "pass": false, "detail": "one-line summary",' in prompt
            assert '"cause": "product" | "qa_capability" | "qa_access"}' in prompt
            assert '"summary": "brief summary"' in prompt
            assert "qa report <file>" in prompt


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
            ' "detail": "404", "cause": "product"}], "summary": "Broken", "state_changes": []}'
        )
        result = parse_qa_result(raw)
        assert result.passed is False
        assert result.blocker is None
        assert result.checks[0]["pass"] is False
        assert result.checks[0]["cause"] == "product"

    @pytest.mark.parametrize("cause", ["qa_capability", "qa_access"])
    def test_a_failed_check_may_name_a_cause_outside_the_product(self, cause):
        raw = json.dumps(
            {
                "pass": False,
                "checks": [{"name": "upload", "pass": False, "detail": "x", "cause": cause}],
                "summary": "not testable",
            }
        )

        result = parse_qa_result(raw)

        assert result.blocker is None
        assert result.checks[0]["cause"] == cause

    @pytest.mark.parametrize("cause", ["qa_capability", "qa_access", "product"])
    def test_a_passing_verdict_with_a_failed_check_is_invalid_and_never_passes(self, cause):
        raw = json.dumps(
            {
                "pass": True,
                "checks": [
                    {"name": "health", "pass": True, "detail": "200"},
                    {
                        "name": "create transaction",
                        "pass": False,
                        "detail": "no tool for POST /api/transactions",
                        "cause": cause,
                    },
                ],
                "summary": "everything QA could test works",
            }
        )

        result = parse_qa_result(raw)

        assert result.passed is False
        assert result.checks == []
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    def test_a_failing_verdict_with_every_check_passed_is_invalid(self):
        raw = json.dumps(
            {
                "pass": False,
                "checks": [{"name": "health", "pass": True, "detail": "200"}],
                "summary": "something felt off",
            }
        )

        result = parse_qa_result(raw)

        assert result.passed is False
        assert result.checks == []
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

    def test_prompt_ties_the_top_level_pass_to_every_check(self):
        prompt = build_qa_prompt(
            acceptance_criteria="- GET /health returns 200", deployed_url="https://a.example"
        )

        assert "Top-level `pass` is false whenever any check failed, whatever its cause." in prompt

    @pytest.mark.parametrize(
        "check",
        [
            {"name": "weather", "pass": False, "detail": "404"},
            {"name": "weather", "pass": False, "detail": "404", "cause": "flaky"},
            {"name": "weather", "pass": False, "detail": "404", "cause": None},
            {"name": "weather", "pass": True, "detail": "200", "cause": "product"},
        ],
        ids=["failed-without-cause", "unknown-cause", "null-cause", "passed-with-cause"],
    )
    def test_a_check_cause_outside_the_contract_is_an_invalid_result(self, check):
        raw = json.dumps({"pass": False, "checks": [check], "summary": "bad"})

        result = parse_qa_result(raw)

        assert result.passed is False
        assert result.checks == []
        assert result.blocker is not None
        assert result.blocker.category == QABlockerCategory.UNKNOWN

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
