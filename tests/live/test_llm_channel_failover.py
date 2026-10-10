"""Stand live test: the LLM channel chain of the Architect and the PO, end to end.

Run on the stand as `stand-e2e.yml suite=custom target=tests/live/test_llm_channel_failover.py`.
It never runs in CI: every offline selection ignores this module, and it spends
real subscription turns and exactly one OpenRouter PO turn.

The four tests are one run, in order, sharing the module fixture `failover`:

1. **healthy** — with the default chain, a brief-backed story is planned by the
   real Architect graph (`stories.planning` names `codex` and no failure), and
   one PO turn is answered by `codex`;
2. **codex_faulted** — with the Architect chain's `codex` entry faulted, a second
   story is planned by `claude`, `codex:<class>` among its failures; the chain is
   restored right after;
3. **no_paid_work** — both stories are archived and no Run exists for either;
4. **subscriptions_faulted** — with the PO chain's `codex` and `claude` faulted
   and langgraph recreated, one PO turn is answered by `openrouter` with the
   degraded note applied and the `subscriptions_down` alert decided; the chain
   is restored and langgraph recreated again.

Faults are agent configuration only (`llm_failover.py` says which, and why).

**Nothing the plan releases can be bought.** The project is created `active`
with no repository and no ready workspace: engineering dispatch refuses it at
`workspace_not_ready` (`engineering_dispatch_admission.py`, rung 3), before the
paid gate, and the scaffold trigger's ensure mode skips a project without a
repository. `work_admission.emergency_stop` does not touch the Architect — it
gates project creation and paid runs only — and is not used: behind a ready
workspace its paid denial parks the story and owes the owner a notice, which is
one more PO turn this run would pay for.

**Restore always.** Every chain is snapshotted before the first leg and put back
in the leg's own `finally`; the fixture's teardown checks all three again and
recreates langgraph if the PO chain was still faulted. `RunClock` stops
productive work early enough that the teardown fits under the stand runner's
custom-target backstop.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import os
import secrets
import subprocess
import time
from typing import Any
import uuid

import httpx
from live_harness import OwnershipManifest, no_cleanup_enabled
from llm_failover import (
    ARCHITECT,
    CHAIN_AGENTS,
    CODEX_FAULTED_BRIEF,
    FAULT_METHOD,
    FAULT_MODEL,
    HEALTHY_BRIEF,
    PO,
    PO_QUESTION,
    SUBSCRIPTIONS_DOWN,
    FailoverBrief,
    FailoverEvidence,
    LegOutcome,
    RunClock,
    alert_decisions,
    alert_outcome,
    architect_codex_faulted_chain,
    codex_faulted_planning_problems,
    configured_channels,
    consumer_group_backlog,
    degraded_turn_problems,
    foreign_channel_answers,
    healthy_planning_problems,
    healthy_turn_problems,
    parse_log_records,
    po_subscriptions_faulted_chain,
    redacted,
    runs_problems,
    turn_channel_lines,
)
from pipeline_helpers import (
    API_URL,
    ORCHESTRATOR_ROOT,
    PO_BRIEF_ID_RE,
    PO_STORY_ID_RE,
    STAGE_NOTICE_KEY_PREFIX,
    STAGE_NOTICE_MARKED_STORIES_KEY,
    TEST_TELEGRAM_ID,
    CleanupError,
    _flat_redis_fields,
    _redis_command,
    _redis_json,
    api_client_as_internal_service,
    api_client_as_test_user,
    api_client_as_unscoped_observer,
    capture_run_po_position,
    cleanup_all,
    ensure_test_user,
    po_tool_boundary,
    record_run_po_position,
    release_project_fences,
    remove_run_po_checkpoints,
)
import pytest
import pytest_asyncio
from run_evidence import evidence_output_directory, write_artifact

from scripts import stand_run
from shared.contracts.dto.llm_channel import LLM_CHANNEL_CHAIN_ADAPTER, LLMChannel
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.po import POResponse, POUserMessage, to_flat_fields
from shared.contracts.worker_evidence import secret_env_values
from shared.live_contour import require_live_contour
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.live_llm_channel_failover,
]

#: The productive window of the whole module. The stand runner kills a custom
#: target at 2700 s; this leaves the teardown — restores, one langgraph
#: recreate, the database teardown — its own 700 s bound and some slack.
PRODUCTIVE_SECONDS = 1900
#: One Architect planning of a one-requirement brief, including a retry the
#: supervisor may re-queue a minute after an incomplete plan.
PLANNING_TIMEOUT_SECONDS = 900
#: One PO turn: a CLI channel gives a user-facing call back after 180 s at most.
PO_TURN_TIMEOUT_SECONDS = 600
#: `po:input` is drained before the paid leg, so no queued event is answered on OpenRouter.
DRAIN_TIMEOUT_SECONDS = 300
#: The alert is scheduled beside the answer and bounded by `ALERT_DEADLINE_SECONDS` (30).
ALERT_WAIT_SECONDS = 45
#: Log lines are flushed a moment after the event that wrote them.
LOG_SETTLE_SECONDS = 15
POLL_SECONDS = 5
LOG_READ_TIMEOUT_SECONDS = 60
LANGGRAPH = "langgraph"
ALERT_DEDUP_KEY = f"llm:alert:{SUBSCRIPTIONS_DOWN}:{PO}"
PAID_RUN_TYPES = ("engineering", "deploy", "qa")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _log_since() -> str:
    """A `docker compose logs --since` value a little before now."""
    return (datetime.now(UTC) - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compose_logs(service: str, since: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["docker", "compose", "logs", "--no-color", "--since", since, service],
        capture_output=True,
        text=True,
        timeout=LOG_READ_TIMEOUT_SECONDS,
        cwd=ORCHESTRATOR_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose logs {service} exited {result.returncode}")
    return parse_log_records(result.stdout)


def _stream_cursor(stream: str) -> str:
    """The last entry id of a stream, before this run can add to it."""
    entries = _redis_json("XREVRANGE", stream, "+", "-", "COUNT", "1")
    return str(entries[0][0]) if entries else "0-0"


def _names_this_run(fields: dict[str, str], project_id: str, story_ids: list[str]) -> bool:
    return fields.get("project_id") == project_id or fields.get("story_id") in story_ids


def _normalized_chain(chain: list | None) -> list[dict] | None:
    """A chain as the API stores it, so a stored and a written one compare."""
    if chain is None:
        return None
    return [
        entry.model_dump(mode="json", exclude_none=True)
        for entry in LLM_CHANNEL_CHAIN_ADAPTER.validate_python(chain)
    ]


class FailoverRun:
    """The module's one run: its clients, its owned project, its evidence."""

    def __init__(self, api: httpx.AsyncClient, internal: httpx.AsyncClient, observer) -> None:
        self.api = api
        self.internal = internal
        self.observer = observer
        self.clock = RunClock(PRODUCTIVE_SECONDS)
        self.manifest = OwnershipManifest(f"live-{uuid.uuid4().hex[:12]}")
        self.ctx: dict[str, Any] = {"manifest": self.manifest}
        self.evidence = FailoverEvidence(run_id=self.manifest.run_id)
        self.story_ids: list[str] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.po_chain_applied = False
        self.alert_key_cleared = False
        self.proactive_cursor = "0-0"

    # ── setup ──────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        await ensure_test_user(self.api, self.internal)
        record_run_po_position(self.ctx, capture_run_po_position(TEST_TELEGRAM_ID))
        self.proactive_cursor = await asyncio.to_thread(_stream_cursor, PO_PROACTIVE_QUEUE)
        for agent in CHAIN_AGENTS:
            self.snapshots[agent] = await self.read_chain(agent)
        self.evidence.chains_before = dict(self.snapshots)

        project_id = str(uuid.uuid4())
        self.manifest.own("project", project_id)
        self.write_manifest()
        self.ctx["project_id"] = project_id
        self.evidence.project_id = project_id
        response = await self.api.post(
            "/api/projects/",
            json={
                "id": project_id,
                "title": f"{require_live_contour().pipeline}-failover-{secrets.token_hex(4)}",
                "initiating_run_id": self.manifest.run_id,
                # Active with no repository: the Architect does not wait for a
                # scaffold, and nothing it plans can be dispatched or scaffolded.
                "status": ProjectStatus.ACTIVE,
                "config": {
                    "description": "LLM channel failover live test",
                    "modules": ["backend"],
                    "agent_type": "noop",
                },
            },
        )
        response.raise_for_status()

    def write_manifest(self) -> None:
        self.manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{self.manifest.run_id}.json")

    # ── legs ──────────────────────────────────────────────────────────────

    @contextmanager
    def leg(self, name: str):
        record = self.evidence.leg(name)
        record["started_at"] = _utc_now()
        try:
            yield record
        except BaseException as exc:
            record["outcome"] = LegOutcome.FAILED.value
            record["failure"] = f"{type(exc).__name__}: {exc}"[:4000]
            raise
        else:
            record["outcome"] = LegOutcome.PASSED.value
        finally:
            record["finished_at"] = _utc_now()

    # ── agent configuration ───────────────────────────────────────────────

    async def read_chain(self, agent: str) -> dict[str, Any]:
        response = await self.internal.get(f"/api/agent-configs/{agent}")
        if response.status_code == httpx.codes.NOT_FOUND:
            return {"exists": False, "llm_channels": None}
        response.raise_for_status()
        return {"exists": True, "llm_channels": response.json().get("llm_channels")}

    async def apply_chain(self, agent: str, chain: list[dict]) -> dict[str, Any]:
        """Write `chain` for `agent` and read it back as the API stores it."""
        if self.snapshots[agent]["exists"]:
            response = await self.internal.patch(
                f"/api/agent-configs/{agent}", json={"llm_channels": chain}
            )
        else:
            response = await self.internal.post(
                "/api/agent-configs/",
                json={
                    "id": agent,
                    "name": f"{agent} (LLM channel failover live test)",
                    "system_prompt": "Only llm_channels is read; this record exists for the test.",
                    "llm_channels": chain,
                },
            )
        response.raise_for_status()
        stored = await self.read_chain(agent)
        if _normalized_chain(stored["llm_channels"]) != _normalized_chain(chain):
            raise AssertionError(f"{agent} chain reads back {stored} after writing {chain}")
        return stored

    async def restore_chain(self, agent: str) -> dict[str, Any]:
        """Put `agent`'s record back exactly as the snapshot found it."""
        before = self.snapshots[agent]
        current = await self.read_chain(agent)
        if current == before:
            return {"restored": "unchanged", "chain": current}
        if not before["exists"]:
            response = await self.internal.delete(f"/api/agent-configs/{agent}")
            if response.status_code not in (httpx.codes.NO_CONTENT, httpx.codes.NOT_FOUND):
                response.raise_for_status()
        else:
            response = await self.internal.patch(
                f"/api/agent-configs/{agent}", json={"llm_channels": before["llm_channels"]}
            )
            response.raise_for_status()
        after = await self.read_chain(agent)
        if after != before:
            raise AssertionError(f"{agent} chain reads {after} after restoring {before}")
        return {"restored": "rewritten", "chain": after}

    async def recreate_langgraph(self) -> dict[str, Any]:
        """Recreate langgraph the stand runner's way and read the PO chain it started with."""
        since = _log_since()
        started = time.monotonic()
        lines: list[str] = []
        ready = await asyncio.to_thread(
            stand_run.recreate_and_wait,
            stand_run.read_env_file(stand_run.REPO / ".env"),
            (LANGGRAPH,),
            lines.append,
        )
        outcome = {
            "ready": ready,
            "seconds": round(time.monotonic() - started, 1),
            "runner_lines": lines,
            "po_channels": None,
        }
        if ready:
            outcome["po_channels"] = configured_channels(_compose_logs(LANGGRAPH, since))
        return outcome

    # ── stories ───────────────────────────────────────────────────────────

    def po_tool_config(self) -> dict[str, Any]:
        return {
            "configurable": {
                "thread_id": self.manifest.run_id,
                "telegram_chat_id": str(TEST_TELEGRAM_ID),
            }
        }

    async def create_brief_story(self, brief: FailoverBrief, record: dict[str, Any]) -> str:
        """A confirmed brief and its story, through the released PO tools; no model."""
        project_id = self.ctx["project_id"]
        config = self.po_tool_config()
        async with po_tool_boundary(api_url=API_URL) as po:
            presented = await po["present_product_brief"].ainvoke(
                brief.present_arguments(project_id), config=config
            )
            brief_match = PO_BRIEF_ID_RE.search(presented)
            if brief_match is None:
                raise AssertionError(f"the PO presented no brief: {presented}")
            record["brief_id"] = brief_match.group(1)
            confirmed = await po["confirm_product_brief"].ainvoke(
                {"project_id": project_id, "brief_id": record["brief_id"]},
                config=config,
            )
            if "confirmed and frozen" not in confirmed:
                raise AssertionError(f"the PO did not freeze the brief: {confirmed}")
            created = await po["create_story"].ainvoke(
                brief.story_arguments(project_id, record["brief_id"]), config=config
            )
        story_match = PO_STORY_ID_RE.search(created)
        if story_match is None:
            raise AssertionError(f"the PO created and published no story: {created}")
        story_id = story_match.group(1)
        self.story_ids.append(story_id)
        record["story_id"] = story_id
        return story_id

    async def wait_planned(self, story_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        """The story's planning record once it is `planned`; raises on a park or the deadline."""
        timeout = self.clock.bound(PLANNING_TIMEOUT_SECONDS)
        deadline = time.monotonic() + timeout
        observations: list[dict[str, Any]] = record.setdefault("planning_observations", [])
        last: Any = object()
        while True:
            response = await self.api.get(f"/api/stories/{story_id}")
            response.raise_for_status()
            story = response.json()
            planning = story.get("planning")
            if planning != last:
                observations.append(
                    {"at": _utc_now(), "status": story.get("status"), "planning": planning}
                )
                last = planning
            state = planning.get("state") if isinstance(planning, dict) else None
            if state == "planned":
                return planning
            if state == "parked":
                raise AssertionError(f"planning of {story_id} was parked: {planning}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"story {story_id} was not planned within {timeout:.0f}s; last {planning}"
                )
            await asyncio.sleep(POLL_SECONDS)

    async def archive_story(self, story_id: str) -> dict[str, Any]:
        """Take the story out of work; a refusal is recorded, the teardown deletes it anyway."""
        read = await self.api.get(f"/api/stories/{story_id}")
        read.raise_for_status()
        status = read.json().get("status")
        if status in (StoryStatus.ARCHIVED.value, StoryStatus.FAILED.value):
            return {"status": status, "archived_by": "already terminal"}
        response = await self.api.post(
            f"/api/stories/{story_id}/archive", json={"actor": "live-test"}
        )
        if response.status_code != httpx.codes.OK:
            return {"status": status, "archive_refused": f"HTTP {response.status_code}"}
        return {"status": response.json().get("status"), "archived_by": "live-test"}

    async def plan_and_archive(self, brief: FailoverBrief, record: dict[str, Any]) -> dict:
        """One brief-backed story planned by the real Architect, then archived at once."""
        story_id = await self.create_brief_story(brief, record)
        try:
            record["planning"] = await self.wait_planned(story_id, record)
        finally:
            record["archive"] = await self.archive_story(story_id)
            record["tasks"] = await self.story_tasks(story_id)
        return record["planning"]

    async def story_tasks(self, story_id: str) -> list[dict[str, Any]]:
        response = await self.api.get("/api/tasks/", params={"project_id": self.ctx["project_id"]})
        response.raise_for_status()
        return [
            {
                key: task.get(key)
                for key in ("id", "title", "status", "dispatch_admitted", "planning_attempt_id")
            }
            for task in response.json()
            if task.get("story_id") == story_id
        ]

    async def runs_by_story(self) -> dict[str, list[dict[str, Any]]]:
        runs: dict[str, list[dict[str, Any]]] = {}
        for story_id in self.story_ids:
            response = await self.observer.get("/api/runs/", params={"story_id": story_id})
            response.raise_for_status()
            runs[story_id] = [
                {key: run.get(key) for key in ("id", "type", "status", "created_at")}
                for run in response.json()
            ]
        response = await self.observer.get(
            "/api/runs/", params={"project_id": self.ctx["project_id"]}
        )
        response.raise_for_status()
        runs["project"] = [
            {key: run.get(key) for key in ("id", "type", "status", "story_id")}
            for run in response.json()
        ]
        return runs

    # ── PO turns ──────────────────────────────────────────────────────────

    async def po_turn(self, leg: str, record: dict[str, Any]) -> dict[str, Any]:
        """One real PO turn for the fixture user; its reply and its channel lines."""
        request_id = f"{self.manifest.run_id}-{leg}"
        since = _log_since()
        message = POUserMessage(
            text=PO_QUESTION,
            telegram_chat_id=str(TEST_TELEGRAM_ID),
            request_id=request_id,
            user_name="live_test_bot",
        )
        fields = [item for pair in to_flat_fields(message).items() for item in pair]
        entry_id = await asyncio.to_thread(_redis_json, "XADD", PO_INPUT_QUEUE, "*", *fields)
        self.manifest.own("redis_entry", str(entry_id), stream=PO_INPUT_QUEUE)
        self.write_manifest()
        turn: dict[str, Any] = {"request_id": request_id, "sent_at": _utc_now()}
        record["po_turn"] = turn

        stream = f"po:response:{request_id}"
        deadline = time.monotonic() + self.clock.bound(PO_TURN_TIMEOUT_SECONDS)
        while True:
            entries = await asyncio.to_thread(_redis_json, "XRANGE", stream, "-", "+")
            if entries:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"PO turn {request_id} got no reply on {stream}")
            await asyncio.sleep(2)
        await asyncio.to_thread(_redis_command, "DEL", stream)
        response = POResponse.model_validate(_flat_redis_fields(entries[0][1]))
        turn["answered_at"] = _utc_now()
        turn["reply_text"] = response.text
        turn["error"] = response.error
        if response.error:
            raise AssertionError(f"PO turn {request_id} answered with an error: {response.text}")

        # The chain logs before the reply is published; the log may flush a moment later.
        settle = time.monotonic() + LOG_SETTLE_SECONDS
        while True:
            records = await asyncio.to_thread(_compose_logs, LANGGRAPH, since)
            turn["channel_lines"] = turn_channel_lines(records, request_id)
            if turn["channel_lines"] or time.monotonic() >= settle:
                break
            await asyncio.sleep(2)
        turn["records_since"] = since
        return turn

    async def drain_po_input(self, record: dict[str, Any]) -> None:
        """Wait until PO has nothing queued or in flight, before OpenRouter can answer it."""
        deadline = time.monotonic() + self.clock.bound(DRAIN_TIMEOUT_SECONDS)
        while True:
            groups = await asyncio.to_thread(_redis_json, "XINFO", "GROUPS", PO_INPUT_QUEUE)
            last_entry_id = await asyncio.to_thread(_stream_cursor, PO_INPUT_QUEUE)
            backlog = consumer_group_backlog(groups, PO_CONSUMER_GROUP, last_entry_id)
            record["po_input_backlog"] = backlog
            if backlog == (0, 0):
                return
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"{PO_INPUT_QUEUE} still had (pending, lag)={backlog} for "
                    f"{PO_CONSUMER_GROUP}; not faulting the PO onto OpenRouter"
                )
            await asyncio.sleep(POLL_SECONDS)

    # ── teardown ──────────────────────────────────────────────────────────

    async def teardown(self) -> None:
        errors: list[str] = []
        cleanup = self.evidence.cleanup

        async def step(name: str, action) -> None:
            try:
                cleanup[name] = await action()
            except Exception as exc:  # noqa: BLE001 — every step runs; all failures are reported
                cleanup[name] = {"error": f"{type(exc).__name__}: {exc}"[:2000]}
                errors.append(f"{name}: {type(exc).__name__}: {exc}")

        for agent in self.snapshots:
            await step(f"restore_{agent}", lambda agent=agent: self.restore_chain(agent))
        if self.po_chain_applied:
            await step("recreate_langgraph", self.recreate_after_restore)
        for story_id in self.story_ids:
            await step(f"archive_{story_id}", lambda s=story_id: self.archive_story(s))
        if self.story_ids:
            await step("runs_by_story", self.runs_by_story)
        if self.alert_key_cleared:
            await step("alert_dedup_key", self._release_alert_key)
        self.evidence.chains_after = {
            agent: cleanup.get(f"restore_{agent}", {}).get("chain") for agent in self.snapshots
        }
        if no_cleanup_enabled():
            cleanup["owned_resources"] = "left in place: LIVE_NO_CLEANUP is set"
        elif "project_id" in self.ctx:
            await step("owned_resources", self._cleanup_owned)
        if errors:
            cleanup["error"] = "; ".join(errors)[:4000]
        self.write_evidence()
        if errors:
            raise CleanupError("LLM failover teardown failed: " + "; ".join(errors))

    async def recreate_after_restore(self) -> dict[str, Any]:
        outcome = await self.recreate_langgraph()
        if not outcome["ready"]:
            raise RuntimeError(f"langgraph was not ready after the restore: {outcome}")
        self.po_chain_applied = False
        return outcome

    async def _release_alert_key(self) -> dict[str, Any]:
        removed = await asyncio.to_thread(_redis_command, "DEL", ALERT_DEDUP_KEY)
        return {"key": ALERT_DEDUP_KEY, "removed": removed}

    def _own_run_stream_entries(self) -> dict[str, int]:
        """Own the PO stream entries this run's stories caused, so teardown XDELs them.

        A story in work owes its owner stage notices (`po:input`), and PO's answer
        to one goes to `po:proactive`, which nothing reads on the stand. Both are
        after the cursors taken at setup and name this run's project or stories.
        """
        owned = {}
        cursors = {
            PO_INPUT_QUEUE: self.ctx.get("run_po_input_cursor") or "0-0",
            PO_PROACTIVE_QUEUE: self.proactive_cursor,
        }
        for stream, cursor in cursors.items():
            entries = _redis_json("XRANGE", stream, f"({cursor}", "+") or []
            mine = [
                entry_id
                for entry_id, fields in entries
                if _names_this_run(
                    _flat_redis_fields(fields), self.ctx["project_id"], self.story_ids
                )
            ]
            for entry_id in mine:
                self.manifest.own("redis_entry", entry_id, stream=stream)
            owned[stream] = len(mine)
        self.write_manifest()
        return owned

    async def _cleanup_owned(self) -> dict[str, Any]:
        stream_entries = await asyncio.to_thread(self._own_run_stream_entries)
        report = await cleanup_all(self.internal, self.observer, self.ctx)
        keys = [f"{STAGE_NOTICE_KEY_PREFIX}{story_id}" for story_id in self.story_ids]
        if keys:
            await asyncio.to_thread(_redis_command, "UNLINK", *keys)
            await asyncio.to_thread(
                _redis_command, "SREM", STAGE_NOTICE_MARKED_STORIES_KEY, *self.story_ids
            )
        release_project_fences(self.ctx)
        remove_run_po_checkpoints(self.ctx)
        return {
            "stream_entries_removed": stream_entries,
            "database": None if report is None else str(report)[:2000],
            "po_checkpoint_removal": self.ctx.get("po_checkpoint_removal"),
        }

    def write_evidence(self) -> None:
        artifact = redacted(
            self.evidence.artifact(generated_at=_utc_now()), secret_env_values(dict(os.environ))
        )
        path = write_artifact(artifact, evidence_output_directory())
        print(f"LLM failover run evidence: {path}", flush=True)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def failover():
    async with (
        api_client_as_test_user(timeout=30) as api,
        api_client_as_internal_service(timeout=30) as internal,
        api_client_as_unscoped_observer(timeout=30) as observer,
    ):
        run = FailoverRun(api, internal, observer)
        try:
            await run.setup()
            yield run
        finally:
            await run.teardown()


class TestLLMChannelFailover:
    """Three legs of the chain and the fence around them, in order."""

    async def test_healthy_chain_plans_and_answers_on_codex(self, failover):
        with failover.leg("healthy") as record:
            record["fault"] = {"method": "none", "chain": "default (no agent config)"}
            for agent in (ARCHITECT, PO):
                assert failover.snapshots[agent]["llm_channels"] is None, (
                    f"{agent} does not run the default chain: {failover.snapshots[agent]}"
                )
            planning = await failover.plan_and_archive(HEALTHY_BRIEF, record)
            assert not healthy_planning_problems(planning), healthy_planning_problems(planning)

            turn = await failover.po_turn("healthy", record)
            problems = healthy_turn_problems(turn["channel_lines"])
            assert not problems, (problems, turn["channel_lines"])

    async def test_codex_faulted_architect_plans_on_claude(self, failover):
        with failover.leg("codex_faulted") as record:
            record["fault"] = {"method": FAULT_METHOD.value, "agent": ARCHITECT}
            record["faulted_chain"] = await failover.apply_chain(
                ARCHITECT, architect_codex_faulted_chain()
            )
            try:
                planning = await failover.plan_and_archive(CODEX_FAULTED_BRIEF, record)
            finally:
                record["restored"] = await failover.restore_chain(ARCHITECT)
            problems = codex_faulted_planning_problems(planning)
            assert not problems, (problems, planning)

    async def test_no_paid_work_was_created_for_the_planned_stories(self, failover):
        with failover.leg("no_paid_work") as record:
            assert len(failover.story_ids) == 2, failover.story_ids
            record["archives"] = {
                story_id: await failover.archive_story(story_id) for story_id in failover.story_ids
            }
            for story_id, archive in record["archives"].items():
                assert archive["status"] == StoryStatus.ARCHIVED.value, (story_id, archive)
            record["runs"] = await failover.runs_by_story()
            problems = runs_problems(record["runs"])
            assert not problems, problems

    async def test_subscriptions_faulted_po_answers_on_openrouter(self, failover):
        with failover.leg("subscriptions_faulted") as record:
            await failover.drain_po_input(record)
            existed = await asyncio.to_thread(_redis_command, "DEL", ALERT_DEDUP_KEY)
            failover.alert_key_cleared = True
            record["alert_dedup_key_cleared"] = {"key": ALERT_DEDUP_KEY, "existed": existed}

            record["fault"] = {"method": FAULT_METHOD.value, "agent": PO}
            failover.po_chain_applied = True
            record["faulted_chain"] = await failover.apply_chain(
                PO, po_subscriptions_faulted_chain()
            )
            try:
                record["recreate"] = await failover.recreate_langgraph()
                assert record["recreate"]["ready"], record["recreate"]
                started_with = record["recreate"]["po_channels"] or []
                assert started_with[:2] == [f"codex:{FAULT_MODEL}", f"claude:{FAULT_MODEL}"], (
                    f"langgraph did not start on the faulted PO chain: {started_with}"
                )
                leg_since = _log_since()
                turn = await failover.po_turn("subscriptions_faulted", record)

                deadline = time.monotonic() + ALERT_WAIT_SECONDS
                while True:
                    records = await asyncio.to_thread(_compose_logs, LANGGRAPH, leg_since)
                    decisions = alert_decisions(records, kind=SUBSCRIPTIONS_DOWN, subject=PO)
                    if decisions or time.monotonic() >= deadline:
                        break
                    await asyncio.sleep(POLL_SECONDS)
                record["alert"] = alert_outcome(decisions)
                foreign = foreign_channel_answers(
                    records, turn["request_id"], LLMChannel.OPENROUTER
                )
                record["foreign_openrouter_answers"] = foreign
            finally:
                record["restored"] = await failover.restore_chain(PO)
                record["restore_recreate"] = await failover.recreate_after_restore()
            restarted_with = record["restore_recreate"]["po_channels"] or []
            assert restarted_with[:2] == ["codex:default", "claude:default"], restarted_with

            problems = degraded_turn_problems(turn["channel_lines"], foreign)
            if not record["alert"]["decided"]:
                problems.append(f"no {SUBSCRIPTIONS_DOWN} alert decision was logged for po")
            assert not problems, (problems, turn["channel_lines"])
