"""The QA capability catalogue: what QA does, how, and what it never does."""

from __future__ import annotations

import re

from shared.contracts.dto.run_result import QAProbePlatform
from shared.contracts.qa_capabilities import (
    QA_ACTIONS,
    QA_NEVER,
    QA_RUNTIME_CALLS,
    QACapabilityPlatform,
    QACapabilityRoute,
    http_write_methods,
    qa_actions,
)
from shared.qa_probe_cli import QA_PROBE_NAME, QA_PROBE_USAGE
from shared.qa_probe_library import seed_probes

_USAGE_CALL = re.compile(rf"^{QA_PROBE_NAME} (?P<call>\w+)", re.MULTILINE)


def _offered(platform: QACapabilityPlatform) -> dict[str, QACapabilityRoute]:
    return {a.action: a.route for a in qa_actions() if a.platform is platform}


class TestEntries:
    def test_every_entry_is_typed_and_worded_on_one_line(self):
        for action in QA_ACTIONS:
            assert isinstance(action.platform, QACapabilityPlatform)
            assert isinstance(action.route, QACapabilityRoute)
            assert action.wording and "\n" not in action.wording, action
            assert (action.call is None) is (action.route is QACapabilityRoute.SANDBOX_PROBE)

    def test_action_names_and_wordings_are_unique(self):
        keys = [(a.platform, a.action) for a in QA_ACTIONS]
        assert len(keys) == len(set(keys))
        assert len({a.wording for a in QA_ACTIONS}) == len(QA_ACTIONS)


class TestTelegram:
    def test_everything_a_user_account_sends_through_telethon_is_offered(self):
        assert _offered(QACapabilityPlatform.TELEGRAM) == {
            "send_text": QACapabilityRoute.TOOL,
            "press_button": QACapabilityRoute.TOOL,
            "send_location": QACapabilityRoute.LIBRARY_SEED,
            "send_contact": QACapabilityRoute.SANDBOX_PROBE,
            "send_media": QACapabilityRoute.SANDBOX_PROBE,
            "reply": QACapabilityRoute.SANDBOX_PROBE,
            "edit": QACapabilityRoute.SANDBOX_PROBE,
        }

    def test_the_location_seed_is_the_ready_library_probe(self):
        seeds = {(s.platform.value, s.name) for s in seed_probes(telegram_bot=True)}
        library = {
            (a.platform.value, a.call)
            for a in QA_ACTIONS
            if a.route is QACapabilityRoute.LIBRARY_SEED
        }

        assert ("telegram", "location") in library
        assert library <= seeds


class TestRoutesAgreeWithTheCli:
    def test_every_tool_call_is_on_the_cli_and_the_cli_has_nothing_unaccounted(self):
        usage = set(_USAGE_CALL.findall(QA_PROBE_USAGE))
        tools = {a.call for a in QA_ACTIONS if a.route is QACapabilityRoute.TOOL}

        assert tools <= usage
        assert usage == tools | QA_RUNTIME_CALLS
        assert not tools & QA_RUNTIME_CALLS

    def test_a_probe_route_names_a_platform_qa_probe_accepts(self):
        probe_platforms = {p.value for p in QAProbePlatform}
        for action in QA_ACTIONS:
            if action.route is not QACapabilityRoute.TOOL:
                assert action.platform.value in probe_platforms, action

    def test_no_web_action_is_offered_while_no_browser_is_installed(self):
        assert _offered(QACapabilityPlatform.WEB) == {}


class TestNever:
    def test_qa_never_writes_through_the_http_api_by_policy(self):
        assert http_write_methods() == {"POST", "PUT", "PATCH", "DELETE"}
        [write] = [n for n in QA_NEVER if n.http_methods]
        assert "policy" in write.reason

    def test_qa_never_touches_product_state_or_anything_outside_its_target(self):
        assert {n.kind for n in QA_NEVER} == {"http_write", "product_state", "outside_target"}
        assert all(n.reason and "\n" not in n.wording for n in QA_NEVER)
