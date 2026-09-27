"""Offline contract of the stand's LLM channel failover test (`llm_failover.py`).

The live module runs only on the stand; what it decides from records and log
lines is pinned here, against the product's own contracts: the chains validate
as the API validates them, a faulted entry is one the chain reports as the
channel it names, and each leg's judge says red for the shapes it exists to
catch.
"""

from __future__ import annotations

import json
from pathlib import Path

import llm_failover as lf
from llm_failover import FaultMethod
import pytest

from scripts import stand_run
from shared.contracts.dto.llm_channel import LLM_CHANNEL_CHAIN_ADAPTER, LLMChannel
from shared.contracts.dto.product_brief import ProposedProductBriefContent

pytestmark = pytest.mark.needs_no_api_credential


def _line(event: str, **fields) -> str:
    return "langgraph-1  | " + json.dumps({"event": event, **fields})


# ── Faulted chains ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", list(FaultMethod))
def test_faulted_chains_validate_as_the_api_stores_them(method):
    architect = LLM_CHANNEL_CHAIN_ADAPTER.validate_python(lf.architect_codex_faulted_chain(method))
    po = LLM_CHANNEL_CHAIN_ADAPTER.validate_python(lf.po_subscriptions_faulted_chain(method))

    assert [entry.channel for entry in architect] == [LLMChannel.CODEX, LLMChannel.CLAUDE]
    assert [entry.channel for entry in po] == [
        LLMChannel.CODEX,
        LLMChannel.CLAUDE,
        LLMChannel.OPENROUTER,
    ]
    faulted = [architect[0], po[0], po[1]]
    if method is FaultMethod.UNKNOWN_MODEL:
        assert all(entry.model == lf.FAULT_MODEL for entry in faulted)
        # A bound well under the PO's 180 s and the Architect's 600 s defaults.
        assert all(entry.timeout_seconds == lf.FAULT_GUARD_SECONDS for entry in faulted)
        assert lf.FAULT_GUARD_SECONDS < 180
    else:
        assert all(entry.timeout_seconds == 1 for entry in faulted)
    # The paid channel is never faulted, and never added to the Architect's leg.
    assert po[2].model is None and po[2].timeout_seconds is None
    assert LLMChannel.OPENROUTER not in {entry.channel for entry in architect}


def test_the_briefs_are_documents_the_po_tool_accepts_and_differ():
    for brief in (lf.HEALTHY_BRIEF, lf.CODEX_FAULTED_BRIEF):
        arguments = brief.present_arguments("00000000-0000-0000-0000-000000000001")
        ProposedProductBriefContent.model_validate(
            {key: value for key, value in arguments.items() if key not in {"project_id", "title"}}
        )
        story = brief.story_arguments("p", "brief-1")
        assert story["product_brief_id"] == "brief-1"
    assert lf.HEALTHY_BRIEF.present_arguments("p") != lf.CODEX_FAULTED_BRIEF.present_arguments("p")


# ── The lines the judge reads are the lines the services write ─────────────

LANGGRAPH_SRC = Path(__file__).resolve().parents[2] / "services" / "langgraph" / "src"


@pytest.mark.parametrize(
    ("source", "events"),
    [
        ("llm/chain.py", (lf.CHANNEL_USED, lf.CHANNEL_FAILED, lf.DEGRADED_NOTE)),
        ("llm/alerts.py", (lf.ALERT_SENT, lf.ALERT_FAILED, lf.SUBSCRIPTIONS_DOWN)),
        ("consumers/po.py", (lf.CHANNELS_CONFIGURED, stand_run.STARTUP_LINES["langgraph"])),
    ],
)
def test_every_event_the_judge_reads_is_logged_by_the_service(source, events):
    text = (LANGGRAPH_SRC / source).read_text(encoding="utf-8")
    for event in events:
        assert f'"{event}"' in text, f"{source} no longer logs {event}"


# ── Log records ──────────────────────────────────────────────────────────────


def test_compose_lines_become_records_and_noise_is_dropped():
    text = "\n".join(
        [
            _line("llm_channel_used", agent="po", channel="codex", request_id="r1"),
            "langgraph-1  | Traceback (most recent call last):",
            'langgraph-1  | {"not an event": 1}',
            "langgraph-1  | {broken json",
        ]
    )

    records = lf.parse_log_records(text)

    assert records == [
        {"event": "llm_channel_used", "agent": "po", "channel": "codex", "request_id": "r1"}
    ]


def test_a_turn_owns_only_the_lines_its_request_id_is_bound_on():
    records = lf.parse_log_records(
        "\n".join(
            [
                _line("llm_channel_failed", agent="po", channel="codex", request_id="mine"),
                _line("llm_channel_used", agent="po", channel="openrouter", request_id="mine"),
                _line("llm_channel_used", agent="po", channel="openrouter", request_id="other"),
                _line("llm_channel_used", agent="po", channel="openrouter"),
                _line("llm_channel_used", agent="po_summarizer", channel="openrouter"),
                _line("llm_channel_used", agent="architect", channel="codex", request_id="mine"),
            ]
        )
    )

    lines = lf.turn_channel_lines(records, "mine")
    foreign = lf.foreign_channel_answers(records, "mine", LLMChannel.OPENROUTER)

    assert [(line["event"], line["channel"]) for line in lines] == [
        ("llm_channel_failed", "codex"),
        ("llm_channel_used", "openrouter"),
    ]
    # Another turn's answer and an event turn's answer are both foreign spend;
    # the summarizer is its own agent and is not a PO turn.
    assert [line.get("request_id") for line in foreign] == ["other", None]


def test_the_started_chain_and_the_alert_decisions_are_read_from_their_own_lines():
    records = lf.parse_log_records(
        "\n".join(
            [
                _line(
                    "po_llm_channels_configured",
                    channels=[
                        f"codex:{lf.FAULT_MODEL}",
                        f"claude:{lf.FAULT_MODEL}",
                        "openrouter:m",
                    ],
                ),
                _line("llm_alert_failed", kind="subscriptions_down", subject="po", delivery="none"),
                _line("llm_alert_sent", kind="payment_required", subject="openrouter"),
            ]
        )
    )

    assert lf.configured_channels(records)[:2] == [
        f"codex:{lf.FAULT_MODEL}",
        f"claude:{lf.FAULT_MODEL}",
    ]
    decisions = lf.alert_decisions(records, kind="subscriptions_down", subject="po")
    assert lf.alert_outcome(decisions) == {
        "decided": True,
        "outcome": "failed",
        "delivery": "none",
        "error_type": None,
        "decisions": decisions,
    }
    assert lf.alert_outcome([])["decided"] is False


@pytest.mark.parametrize(
    ("groups", "last_entry_id", "backlog"),
    [
        ([["name", "po-consumer", "pending", 0, "lag", 0]], "5-0", (0, 0)),
        ([{"name": "po-consumer", "pending": 2, "lag": 1}], "5-0", (2, 1)),
        # A deleted entry makes Redis give up on the lag; delivery position decides.
        (
            [["name", "po-consumer", "pending", 0, "lag", None, "last-delivered-id", "5-0"]],
            "5-0",
            (0, 0),
        ),
        (
            [["name", "po-consumer", "pending", 0, "lag", None, "last-delivered-id", "4-9"]],
            "5-0",
            (0, 1),
        ),
        ([["name", "po-consumer", "pending", 0, "lag", None]], "5-0", None),
        ([["name", "other", "pending", 0, "lag", 0]], "5-0", None),
        (None, "0-0", None),
    ],
)
def test_the_po_consumer_backlog_is_read_in_both_redis_cli_shapes(groups, last_entry_id, backlog):
    assert lf.consumer_group_backlog(groups, "po-consumer", last_entry_id) == backlog


# ── What each leg owes ───────────────────────────────────────────────────────


def _planning(channels, failures, state="planned"):
    return {"state": state, "channels": channels, "channel_failures": failures}


def test_a_healthy_plan_is_codex_alone_with_no_failure():
    assert lf.healthy_planning_problems(_planning(["codex"], [])) == []
    assert lf.healthy_planning_problems(_planning(["codex", "claude"], ["codex:timeout"]))
    assert lf.healthy_planning_problems(_planning(["codex"], [], state="retrying"))
    assert lf.healthy_planning_problems(None)


def test_a_codex_faulted_plan_is_claude_with_a_classified_codex_failure():
    good = _planning(["claude"], ["codex:nonzero_exit", "codex:nonzero_exit"])
    assert lf.codex_faulted_planning_problems(good) == []

    silently_accepted = lf.codex_faulted_planning_problems(_planning(["codex"], []))
    assert any("FaultMethod.TIMEOUT" in problem for problem in silently_accepted)
    assert lf.codex_faulted_planning_problems(_planning(["claude"], []))
    assert lf.codex_faulted_planning_problems(_planning(["claude", "openrouter"], ["codex:x"]))


def test_a_healthy_turn_is_answered_by_codex_alone():
    used = {"event": "llm_channel_used"}
    assert lf.healthy_turn_problems([{**used, "channel": "codex"}]) == []
    assert lf.healthy_turn_problems([{**used, "channel": "claude"}])
    assert lf.healthy_turn_problems([])


def test_a_degraded_turn_needs_both_failures_openrouter_the_note_and_no_foreign_spend():
    lines = [
        {"event": "llm_channel_failed", "channel": "codex"},
        {"event": "llm_channel_failed", "channel": "claude"},
        {"event": "llm_degraded_note_added"},
        {"event": "llm_channel_used", "channel": "openrouter"},
    ]
    assert lf.degraded_turn_problems(lines, []) == []

    without_note = [line for line in lines if line["event"] != "llm_degraded_note_added"]
    assert any("note" in problem for problem in lf.degraded_turn_problems(without_note, []))
    without_claude = [line for line in lines if line.get("channel") != "claude"]
    assert lf.degraded_turn_problems(without_claude, [])
    assert lf.degraded_turn_problems(lines, [{"channel": "openrouter", "request_id": "x"}])


def test_any_run_of_a_planned_story_is_named():
    assert lf.runs_problems({"story-1": [], "project": []}) == []
    assert lf.runs_problems({"story-1": [{"id": "eng-1", "type": "engineering"}]}) == [
        "story story-1 has engineering run eng-1 (None)"
    ]


# ── Clock and evidence ───────────────────────────────────────────────────────


def test_the_run_clock_shortens_every_wait_to_what_is_left():
    now = [100.0]
    clock = lf.RunClock(60, now=lambda: now[0])
    assert clock.bound(900) == 60
    now[0] = 150.0
    assert clock.bound(900) == 10
    assert clock.bound(5) == 5
    now[0] = 500.0
    assert clock.bound(900) == 0


def test_the_artifact_is_admissible_and_red_for_an_unreached_leg():
    evidence = lf.FailoverEvidence(run_id="live-abc")
    for leg in lf.LEGS[:-1]:
        evidence.leg(leg)["outcome"] = "passed"

    artifact = evidence.artifact(generated_at="2026-09-27T01:02:03.456789+00:00")

    assert artifact["combination"]["label"] == lf.EVIDENCE_LABEL
    assert artifact["verdict"] == {
        "status": "red",
        "paid": False,
        "reasons": ["subscriptions_faulted: not reached"],
    }
    assert artifact["failure"]["failed"] is True
    assert artifact["fault_method"]["model"] == lf.FAULT_MODEL

    evidence.leg(lf.LEGS[-1])["outcome"] = "passed"
    green = evidence.artifact(generated_at="2026-09-27T01:02:03+00:00")
    assert green["verdict"]["status"] == "green"
    assert green["failure"] == {"failed": False, "stage": "completed", "reason": None}


def test_the_artifact_name_is_one_stand_acceptance_admits(tmp_path):
    from run_evidence import write_artifact

    from scripts.stand_acceptance import RUN_EVIDENCE, _paid_failure_errors

    artifact = lf.FailoverEvidence(run_id="live-abc").artifact(
        generated_at="2026-09-27T01:02:03.456789+00:00"
    )
    path = write_artifact(artifact, tmp_path)

    assert RUN_EVIDENCE.fullmatch(path.name), path.name
    assert _paid_failure_errors(path.name, artifact) == []


def test_every_string_of_the_evidence_is_redacted():
    evidence = {"reply": "token s3cr3t-value here", "lines": [{"reason": "s3cr3t-value"}], "n": 1}

    assert lf.redacted(evidence, ["s3cr3t-value"]) == {
        "reply": "token [redacted] here",
        "lines": [{"reason": "[redacted]"}],
        "n": 1,
    }
