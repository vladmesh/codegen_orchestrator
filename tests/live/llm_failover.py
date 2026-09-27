"""What the stand's LLM channel failover test decides, apart from the stand itself.

`test_llm_channel_failover.py` drives three legs on the stand — the default
chain, the Architect with `codex` faulted, the PO with `codex` and `claude`
faulted — and judges each by durable records and by the channel lines the
services log. Everything that judge needs and that can be decided without a
stand lives here, so `test_llm_failover_contract.py` pins it offline: the
faulted chains, the brief the Architect plans, how a compose log line becomes a
record, which records belong to one PO turn, and what each leg owes.

**The fault is configuration, never code.** A faulted channel is a chain entry
naming a model its CLI rejects (`FAULT_MODEL`). That is a real CLI failure — the
subscription CLI exits non-zero or reports an error envelope — classified by the
chain like any other. Claude Code 2.x answers such a model with
`api_error_status: 404` and exit 1. Should a CLI ever accept the name silently,
the faulted channel answers, and the leg names that as the finding instead of
passing: the fallback is `timeout_seconds: 1` on the same entry
(`FaultMethod.TIMEOUT`), a one-constant switch. The unknown-model entry also
carries `FAULT_GUARD_SECONDS`, a bound rather than the fault.

**The Architect's faulted chain has no `openrouter` entry.** The stand's
OpenRouter balance buys exactly one PO turn. A codex-faulted planning that
`claude` also failed must end red, not be planned on OpenRouter.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
import json
import time
from typing import Any

from shared.contracts.dto.llm_channel import LLMChannel

#: The model name every faulted CLI entry asks for.
FAULT_MODEL = "nonexistent-model-for-failover-test"
#: A bound on the unknown-model entry, not the fault: a CLI that rejects the
#: model exits within seconds and the chain records its own class
#: (`nonzero_exit`, `invalid_output`). Without it a CLI that kept retrying the
#: rejected model would hold every Architect call for the 600 s service default.
#: Should it ever fire, the failure reads `timeout` and the evidence says so.
FAULT_GUARD_SECONDS = 120


class FaultMethod(StrEnum):
    """How a chain entry is made to fail."""

    UNKNOWN_MODEL = "unknown_model"
    TIMEOUT = "timeout"


FAULT_METHOD = FaultMethod.UNKNOWN_MODEL

#: The agent-config ids the chains live under (`services/langgraph/src/llm/vocab.py`).
ARCHITECT = "architect"
PO = "po"
PO_SUMMARIZER = "po_summarizer"
CHAIN_AGENTS = (ARCHITECT, PO, PO_SUMMARIZER)

#: The user message of every PO turn this test pays for.
PO_QUESTION = "What is the status of my project?"

#: The label of the run-evidence artifact; `stand_acceptance.RUN_EVIDENCE` admits it.
EVIDENCE_LABEL = "llm-channel-failover"

#: The log events of one channel-chain call (`services/langgraph/src/llm/chain.py`).
CHANNEL_USED = "llm_channel_used"
CHANNEL_FAILED = "llm_channel_failed"
DEGRADED_NOTE = "llm_degraded_note_added"
ALERT_SENT = "llm_alert_sent"
ALERT_FAILED = "llm_alert_failed"
SUBSCRIPTIONS_DOWN = "subscriptions_down"
CHANNELS_CONFIGURED = "po_llm_channels_configured"
CHANNEL_EVENTS = frozenset({CHANNEL_USED, CHANNEL_FAILED, DEGRADED_NOTE})
ALERT_EVENTS = frozenset({ALERT_SENT, ALERT_FAILED})

#: The fields a channel line keeps in the evidence; the rest is service noise.
_CHANNEL_FIELDS = (
    "event",
    "agent",
    "channel",
    "model",
    "position",
    "failure_class",
    "http_status",
    "reason",
    "duration_s",
    "request_id",
    "timestamp",
)
_ALERT_FIELDS = ("event", "kind", "subject", "delivery", "error_type", "agent", "request_id")


def fault_entry(channel: LLMChannel, method: FaultMethod = FAULT_METHOD) -> dict[str, Any]:
    """One chain entry that makes `channel` fail every call."""
    if method is FaultMethod.TIMEOUT:
        return {"channel": channel.value, "timeout_seconds": 1}
    return {"channel": channel.value, "model": FAULT_MODEL, "timeout_seconds": FAULT_GUARD_SECONDS}


def architect_codex_faulted_chain(method: FaultMethod = FAULT_METHOD) -> list[dict[str, Any]]:
    """`codex` faulted, `claude` as configured by default, and no paid channel."""
    return [fault_entry(LLMChannel.CODEX, method), {"channel": LLMChannel.CLAUDE.value}]


def po_subscriptions_faulted_chain(method: FaultMethod = FAULT_METHOD) -> list[dict[str, Any]]:
    """Both subscription channels faulted, `openrouter` on the agent's env model."""
    return [
        fault_entry(LLMChannel.CODEX, method),
        fault_entry(LLMChannel.CLAUDE, method),
        {"channel": LLMChannel.OPENROUTER.value},
    ]


# ── The briefs the Architect plans ───────────────────────────────────────────


@dataclass(frozen=True)
class FailoverBrief:
    """One small confirmed brief: one backend requirement, one usage example.

    Small on purpose: the planning turn is what is measured, and every extra
    requirement is more model turns on a stand clock. The two legs present
    different documents, because the PO tool keys a presentation on the
    document and would hand back the first revision otherwise.
    """

    requirement_id: str
    title: str
    summary: str
    requirement: str
    user_wording: str
    user_sends: str
    product_answers: str
    story_title: str

    def present_arguments(self, project_id: str) -> dict[str, Any]:
        """Exactly what `present_product_brief` is called with."""
        return {
            "project_id": project_id,
            "title": self.title,
            "summary": self.summary,
            "must_requirements": [
                {
                    "id": self.requirement_id,
                    "text": self.requirement,
                    "user_wording": self.user_wording,
                }
            ],
            "language": "en",
            "usage_examples": [
                {
                    "requirement_id": self.requirement_id,
                    "user_sends": self.user_sends,
                    "product_answers": self.product_answers,
                }
            ],
            "limitations": ["The endpoint is public: there is no authentication."],
        }

    def story_arguments(self, project_id: str, brief_id: str) -> dict[str, Any]:
        """Exactly what `create_story` is called with."""
        return {
            "project_id": project_id,
            "title": self.story_title,
            "description": f"{self.summary} {self.requirement}",
            "product_brief_id": brief_id,
        }


HEALTHY_BRIEF = FailoverBrief(
    requirement_id="ping",
    title="Ping service",
    summary="A backend service that answers a liveness ping.",
    requirement="GET /ping answers 200 with the plain text pong.",
    user_wording="I want a ping endpoint that says pong.",
    user_sends="a GET request to /ping",
    product_answers="the text pong",
    story_title="Ping endpoint",
)

CODEX_FAULTED_BRIEF = FailoverBrief(
    requirement_id="version",
    title="Version service",
    summary="A backend service that reports its version.",
    requirement='GET /version answers 200 with the JSON object {"version": "1"}.',
    user_wording="I want to ask the service which version it runs.",
    user_sends="a GET request to /version",
    product_answers='the JSON {"version": "1"}',
    story_title="Version endpoint",
)


# ── Compose logs as records ─────────────────────────────────────────────────


def parse_log_records(text: str) -> list[dict[str, Any]]:
    """Every JSON structlog record in `docker compose logs` output, in order.

    Compose prefixes each line with the container name; the record is the JSON
    object that starts after it. A line that is not one is service noise.
    """
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        start = line.find('{"')
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str):
            records.append(record)
    return records


def _compact(record: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {name: record[name] for name in fields if record.get(name) is not None}


def turn_channel_lines(
    records: Iterable[dict[str, Any]], request_id: str, *, agent: str = PO
) -> list[dict[str, Any]]:
    """The channel lines of one PO turn: its request id is bound on every one."""
    return [
        _compact(record, _CHANNEL_FIELDS)
        for record in records
        if record.get("event") in CHANNEL_EVENTS
        and record.get("request_id") == request_id
        and record.get("agent") == agent
    ]


def foreign_channel_answers(
    records: Iterable[dict[str, Any]], request_id: str, channel: LLMChannel, *, agent: str = PO
) -> list[dict[str, Any]]:
    """`channel` answers of `agent` that belong to any other turn than this one."""
    return [
        _compact(record, _CHANNEL_FIELDS)
        for record in records
        if record.get("event") == CHANNEL_USED
        and record.get("agent") == agent
        and record.get("channel") == channel.value
        and record.get("request_id") != request_id
    ]


def alert_decisions(
    records: Iterable[dict[str, Any]], *, kind: str, subject: str
) -> list[dict[str, Any]]:
    """Every sent or failed delivery of one alert, as `alerts.py` logs it."""
    return [
        _compact(record, _ALERT_FIELDS)
        for record in records
        if record.get("event") in ALERT_EVENTS
        and record.get("kind") == kind
        and record.get("subject") == subject
    ]


def configured_channels(records: Iterable[dict[str, Any]]) -> list[str] | None:
    """The PO chain the langgraph process started with (`channel:model` each)."""
    for record in records:
        if record.get("event") == CHANNELS_CONFIGURED and isinstance(record.get("channels"), list):
            return [str(channel) for channel in record["channels"]]
    return None


def _stream_id(value: Any) -> tuple[int, int] | None:
    try:
        milliseconds, sequence = str(value).split("-")
        return int(milliseconds), int(sequence)
    except ValueError:
        return None


def consumer_group_backlog(groups: Any, group: str, last_entry_id: str) -> tuple[int, int] | None:
    """`(pending, lag)` of one consumer group from `XINFO GROUPS` JSON, or None.

    `redis-cli --json` renders each group as a flat `[key, value, ...]` array
    (RESP2) or an object (RESP3); both are read. Redis reports a `null` lag once
    an entry was deleted from the range it counts; the group is then caught up
    exactly when it was delivered the stream's last entry (`last_entry_id`,
    `0-0` for an empty stream). Anything else is unknown, never "no backlog".
    """
    if not isinstance(groups, list):
        return None
    for entry in groups:
        if isinstance(entry, list):
            fields = dict(zip(entry[::2], entry[1::2], strict=False))
        elif isinstance(entry, dict):
            fields = entry
        else:
            continue
        if fields.get("name") != group:
            continue
        pending, lag = fields.get("pending"), fields.get("lag")
        if not isinstance(pending, int):
            return None
        if isinstance(lag, int):
            return pending, lag
        delivered = _stream_id(fields.get("last-delivered-id"))
        last = _stream_id(last_entry_id)
        if delivered is None or last is None:
            return None
        return pending, 0 if delivered >= last else 1
    return None


# ── What each leg owes ──────────────────────────────────────────────────────


def healthy_planning_problems(planning: dict[str, Any] | None) -> list[str]:
    """A default-chain plan: planned, answered by `codex` alone, no failed channel."""
    if not isinstance(planning, dict):
        return ["the story carries no planning record"]
    problems = []
    if planning.get("state") != "planned":
        problems.append(f"planning state is {planning.get('state')!r}, not 'planned'")
    if planning.get("channels") != [LLMChannel.CODEX.value]:
        problems.append(f"planning channels are {planning.get('channels')!r}, not ['codex']")
    if planning.get("channel_failures"):
        problems.append(f"a channel failed: {planning.get('channel_failures')!r}")
    return problems


def codex_faulted_planning_problems(planning: dict[str, Any] | None) -> list[str]:
    """A codex-faulted plan: planned by `claude`, and `codex:<class>` among the failures."""
    if not isinstance(planning, dict):
        return ["the story carries no planning record"]
    problems = []
    if planning.get("state") != "planned":
        problems.append(f"planning state is {planning.get('state')!r}, not 'planned'")
    channels = planning.get("channels") or []
    if LLMChannel.CODEX.value in channels:
        problems.append(
            f"codex answered despite its fault ({FAULT_METHOD.value}): {channels!r}; a CLI that "
            "accepts the fault silently needs FaultMethod.TIMEOUT"
        )
    if channels != [LLMChannel.CLAUDE.value]:
        problems.append(f"planning channels are {channels!r}, not ['claude']")
    failures = planning.get("channel_failures") or []
    codex_failures = [entry for entry in failures if str(entry).startswith("codex:")]
    if not codex_failures:
        problems.append(f"no codex:<failure class> among the failures: {failures!r}")
    return problems


def healthy_turn_problems(lines: list[dict[str, Any]]) -> list[str]:
    """A default-chain PO turn: every answer is `codex`'s."""
    answered = [line.get("channel") for line in lines if line.get("event") == CHANNEL_USED]
    if not answered:
        return ["no llm_channel_used line names this PO turn"]
    if set(answered) != {LLMChannel.CODEX.value}:
        return [f"the turn was answered by {answered!r}, not codex alone"]
    return []


def degraded_turn_problems(
    lines: list[dict[str, Any]], foreign_openrouter: list[dict[str, Any]]
) -> list[str]:
    """A subscriptions-faulted PO turn: codex and claude fail, openrouter answers, noted."""
    problems = []
    failed = {line.get("channel") for line in lines if line.get("event") == CHANNEL_FAILED}
    for channel in (LLMChannel.CODEX, LLMChannel.CLAUDE):
        if channel.value not in failed:
            problems.append(f"no llm_channel_failed line for {channel.value}")
    answered = [line.get("channel") for line in lines if line.get("event") == CHANNEL_USED]
    if not answered:
        problems.append("no llm_channel_used line names this PO turn")
    elif set(answered) != {LLMChannel.OPENROUTER.value}:
        problems.append(f"the turn was answered by {answered!r}, not openrouter alone")
    if not any(line.get("event") == DEGRADED_NOTE for line in lines):
        problems.append(f"no {DEGRADED_NOTE} line: the PO emergency note was not applied")
    if foreign_openrouter:
        problems.append(
            f"{len(foreign_openrouter)} openrouter PO answer(s) belong to no turn of this test; "
            "the stand's balance allows exactly one OpenRouter PO turn"
        )
    return problems


def alert_outcome(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """The subscriptions-down alert as decided: sent, or failed with its delivery."""
    if not decisions:
        return {"decided": False, "outcome": None, "decisions": []}
    sent = [decision for decision in decisions if decision.get("event") == ALERT_SENT]
    chosen = sent[0] if sent else decisions[0]
    return {
        "decided": True,
        "outcome": "sent" if sent else "failed",
        "delivery": chosen.get("delivery"),
        "error_type": chosen.get("error_type"),
        "decisions": decisions,
    }


def runs_problems(runs_by_story: dict[str, list[dict[str, Any]]]) -> list[str]:
    """No engineering, deploy or QA Run exists for a story this test planned."""
    return [
        f"story {story_id} has {run.get('type')} run {run.get('id')} ({run.get('status')})"
        for story_id, runs in runs_by_story.items()
        for run in runs
    ]


# ── The run's clock and its evidence ────────────────────────────────────────


class RunClock:
    """One productive deadline for the whole module, so teardown always fits.

    Every wait asks for `bound(own_timeout)` and gets whichever is shorter: its
    own timeout or what is left of the run. The stand runner kills a custom
    target at `CUSTOM_TARGET_TIMEOUT_SECONDS`; a fixture whose restore never ran
    would leave the next suite on a faulted chain.
    """

    def __init__(self, productive_seconds: float, *, now=time.monotonic) -> None:
        self._now = now
        self.deadline = now() + productive_seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - self._now())

    def bound(self, timeout: float) -> float:
        return min(timeout, self.remaining())


class LegOutcome(StrEnum):
    NOT_REACHED = "not_reached"
    PASSED = "passed"
    FAILED = "failed"


#: The legs, in the order the module runs them.
LEGS = ("healthy", "codex_faulted", "no_paid_work", "subscriptions_faulted")


@dataclass
class FailoverEvidence:
    """Everything the run-evidence artifact carries, filled in as the legs run."""

    run_id: str
    project_id: str | None = None
    chains_before: dict[str, Any] = field(default_factory=dict)
    chains_after: dict[str, Any] = field(default_factory=dict)
    legs: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {leg: {"outcome": LegOutcome.NOT_REACHED.value} for leg in LEGS}
    )
    cleanup: dict[str, Any] = field(default_factory=dict)

    def leg(self, name: str) -> dict[str, Any]:
        return self.legs[name]

    def artifact(self, *, generated_at: str) -> dict[str, Any]:
        failed = [name for name, leg in self.legs.items() if leg["outcome"] == "failed"]
        unreached = [name for name, leg in self.legs.items() if leg["outcome"] == "not_reached"]
        cleanup_error = self.cleanup.get("error")
        reasons = [f"{name}: {self.legs[name].get('failure')}" for name in failed]
        reasons += [f"{name}: not reached" for name in unreached]
        if cleanup_error:
            reasons.append(f"cleanup: {cleanup_error}")
        stage = failed[0] if failed else ("cleanup" if cleanup_error else "completed")
        return {
            "schema_version": 1,
            "kind": "llm_channel_failover",
            "generated_at": generated_at,
            "combination": {"label": EVIDENCE_LABEL},
            "run_id": self.run_id,
            "project_id": self.project_id,
            "fault_method": {
                "method": FAULT_METHOD.value,
                "model": FAULT_MODEL if FAULT_METHOD is FaultMethod.UNKNOWN_MODEL else None,
                "timeout_seconds": fault_entry(LLMChannel.CODEX)["timeout_seconds"],
                "architect_chain": architect_codex_faulted_chain(),
                "po_chain": po_subscriptions_faulted_chain(),
            },
            "chains": {"before": self.chains_before, "after": self.chains_after},
            "legs": self.legs,
            "cleanup": self.cleanup,
            "failure": {
                "failed": bool(reasons),
                "stage": stage,
                "reason": "; ".join(reasons) or None,
            },
            # No engineering or QA worker is started, so none of the paid
            # worker retention `stand_acceptance` demands of a paid run exists.
            "verdict": {
                "status": "red" if reasons else "green",
                "paid": False,
                "reasons": reasons,
            },
        }


def redacted(value: Any, secrets: Iterable[str]) -> Any:
    """`value` with every string passed through `redact_diagnostic`."""
    from shared.diagnostics import redact_diagnostic

    known = tuple(secret for secret in secrets if secret)
    if isinstance(value, str):
        return redact_diagnostic(value, secrets=known)
    if isinstance(value, dict):
        return {key: redacted(item, known) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redacted(item, known) for item in value]
    return value
