"""Bounded settings-seed follow-up policy for the live Product Brief harness.

Only the deploy-result wait is injected by ``pipeline_helpers`` because it
combines deploy-Run discovery and outcome evidence collection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import time
from typing import Any, NamedTuple, Protocol

import httpx
from live_harness import TERMINAL_RUN_STATUSES, run_created_at
from pydantic import ValidationError

from shared.contracts.dto.run_result import (
    DeployRunResult,
    EngineeringRunResult,
    deploy_fix_run_id,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.deploy import DeployOutcome

DEPLOY_MAX_FIX_ATTEMPTS_CONFIG_KEY = "deploy.max_deploy_fix_attempts"
DEPLOY_MAX_RETRIES_CONFIG_KEY = "deploy.max_deploy_retries"
#: Ceiling on how long the story gate may reuse an affirmative answer. The gate
#: caches to spare the API a read per wait, never to make a terminal refusal
#: arrive late, so the effective cadence is the smaller of this and the poll
#: interval of the wait the gate serves — see `_story_alive_gate`.
SETTINGS_SEED_STORY_POLL_INTERVAL = 30


class WaitOutcome(NamedTuple):
    """What one pass over a wait's fact sources found.

    `settled` false means "nothing terminal, keep waiting", and only then may the
    wait consult its clock. On a settled outcome exactly one of the other two is
    meaningful: `value` is the fact the wait was watching for, `reason` is a
    named refusal the source produced instead, and both being empty means the
    source recorded its own reason in the run's evidence as it read — which is
    what the story gate does. On an unsettled outcome `value` may carry whatever
    the source last read, for a caller that records it.
    """

    settled: bool
    value: Any = None
    reason: str | None = None


#: Nothing terminal on this pass. A wait may look at its deadline only after a
#: pass has returned this, which is what makes a timeout reason true by
#: construction: every source was read and none of them answered.
KEEP_WAITING = WaitOutcome(settled=False)

#: The story gate refused and recorded its own reason while reading the story.
STORY_REFUSED = WaitOutcome(settled=True)


def observed(value: Any) -> WaitOutcome:
    """A source produced the fact this wait was watching for."""
    return WaitOutcome(settled=True, value=value)


def refused(reason: str) -> WaitOutcome:
    """A source produced a named terminal refusal instead of that fact."""
    return WaitOutcome(settled=True, reason=reason)


async def resolve_wait_pass(
    *,
    run_fact: Callable[[], Awaitable[WaitOutcome]],
    story_alive: Callable[[], Awaitable[bool]] | None,
    on_poll: Callable[[], None] | None,
) -> WaitOutcome:
    """Consult every fact source of one wait, before its clock is allowed to answer.

    Each wait on this path hand-rolled its own sequence of deadline test, Run
    read and story read, so the rule "an expired deadline still reads first" had
    to be re-implemented at every site — and a site that implemented it for one
    of its two sources looked finished. It lives here instead: a wait describes
    its sources, this performs the pass, and the wait's only remaining decision
    is what to do with `KEEP_WAITING`.

    The Run wins over the story, and wins by not consulting it: `4a05172` parks a
    story *because* its engineering result carried no new commit, so the Run's
    typed result is the proximate cause and the story's status is the
    consequence. An artifact naming the consequence would send its reader to the
    wrong place. The clock is last, and only when neither source answered.
    """
    if on_poll is not None:
        on_poll()
    fact = await run_fact()
    if fact.settled:
        return fact
    if story_alive is not None and not await story_alive():
        return STORY_REFUSED
    # The unsettled fact itself, not the bare constant: a source that read
    # something without settling keeps that payload for a caller that records it.
    return fact


def settle(ctx: dict, outcome: WaitOutcome) -> Any:
    """Record the refusal a settled pass carried, and hand back the fact it found."""
    if outcome.reason is not None:
        ctx["settings_seed_repair_error"] = outcome.reason
    return outcome.value


class FollowupDeployWait(Protocol):
    """The harness operation that observes and types one fresh deploy Run."""

    async def __call__(
        self,
        api_internal: httpx.AsyncClient,
        ctx: dict,
        *,
        deadline: float,
        created_after: datetime,
        poll_interval: float,
        on_poll: Callable[[], None] | None,
        story_alive: Callable[[], Awaitable[bool]],
    ) -> DeployRunResult | None: ...


async def _runtime_positive_int(api_internal: httpx.AsyncClient, key: str) -> int:
    response = await api_internal.get(f"/api/system-configs/{key}")
    response.raise_for_status()
    value = response.json().get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RuntimeError(f"system config {key} must be a positive integer, got {value!r}")
    return value


async def _settings_seed_runtime_cap(
    api_internal: httpx.AsyncClient, ctx: dict, key: str
) -> int | None:
    """Fail closed with retained evidence if a scheduler ceiling is unreadable."""
    try:
        return await _runtime_positive_int(api_internal, key)
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        ctx["settings_seed_repair_error"] = (
            f"settings-seed follow-up could not read scheduler config {key}: "
            f"{type(error).__name__}: {error}"
        )
        return None


def _settings_seed_source_created_at(ctx: dict) -> datetime | None:
    """Turn malformed source Run timing into retained terminal evidence."""
    try:
        return run_created_at(
            {"id": ctx["deploy_run_id"], "created_at": ctx.get("deploy_run_created_at")}
        )
    except (KeyError, ValueError) as error:
        ctx["settings_seed_repair_error"] = (
            f"settings-seed follow-up source deploy timestamp is invalid: "
            f"{type(error).__name__}: {error}"
        )
        return None


def _story_alive_gate(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    attempt: int | None,
    *,
    poll_interval: float,
) -> Callable[[], Awaitable[bool]]:
    """Return one cached-cadence check shared by every wait in one follow-up.

    The gate is what turns a story refusal into a named exit, so it may never be
    slower than the wait it gates: a cache longer than the poll interval would
    answer two or three polls from a story state the control plane had already
    left, and the card promises the refusal within one poll. The cache therefore
    holds for at most the wait's own interval. The gate exists only while a
    repair is outstanding, so the extra reads are a handful.
    """
    cadence = min(SETTINGS_SEED_STORY_POLL_INTERVAL, poll_interval)
    next_poll = 0.0

    async def story_alive() -> bool:
        nonlocal next_poll
        if time.monotonic() < next_poll:
            return True
        story_id = ctx["story_id"]
        response = await api_internal.get(f"/api/stories/{story_id}")
        response.raise_for_status()
        status = response.json().get("status")
        ctx["settings_seed_repair_story_status"] = status
        next_poll = time.monotonic() + cadence
        if status not in {StoryStatus.FAILED.value, StoryStatus.WAITING_HUMAN_REVIEW.value}:
            return True
        suffix = f" before manifest repair attempt {attempt}" if attempt is not None else ""
        ctx["settings_seed_repair_error"] = f"story {story_id} reached {status}{suffix}"
        return False

    return story_alive


async def _wait_for_manifest_repair_run(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    source_run_id: str,
    attempt: int,
    deadline: float,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    story_alive: Callable[[], Awaitable[bool]],
) -> dict | None:
    """Wait for the scheduler-owned repair Run or its terminal story refusal.

    Two fact sources: the repair Run this attempt owns, and the story that owns
    the repair. Both go through `resolve_wait_pass`, so the deadline below is
    reached only after a pass read both and neither answered.
    """
    story_id = ctx["story_id"]
    repair_run_id = deploy_fix_run_id(source_run_id, attempt)

    async def repair_run_fact() -> WaitOutcome:
        response = await api_internal.get(f"/api/runs/{repair_run_id}")
        if response.status_code == 404:
            return KEEP_WAITING
        response.raise_for_status()
        run = response.json()
        if (
            run.get("story_id") != story_id
            or (run.get("run_metadata") or {}).get("deploy_fix_attempt") != attempt
        ):
            return refused(
                f"manifest repair Run {repair_run_id} does not name story {story_id} "
                f"attempt {attempt}"
            )
        return observed(run)

    while True:
        outcome = await resolve_wait_pass(
            run_fact=repair_run_fact, story_alive=story_alive, on_poll=on_poll
        )
        if outcome.settled:
            return settle(ctx, outcome)
        if time.monotonic() >= deadline:
            ctx["settings_seed_repair_error"] = (
                f"no manifest repair attempt {attempt} appeared for story {story_id} "
                "before the repair deadline"
            )
            return None
        await asyncio.sleep(poll_interval)


async def _read_run(api_internal: httpx.AsyncClient, run_id: str) -> dict:
    """One Run as the control plane currently reports it."""
    response = await api_internal.get(f"/api/runs/{run_id}")
    response.raise_for_status()
    return response.json()


def _repair_failure_classification(run: dict) -> str | None:
    """The typed reason a repair Run recorded for producing nothing usable.

    ``EngineeringRunResult.failure_reason`` is the name the pipeline itself gave
    the refusal — `no_new_commit` above all — and it is what a red artifact needs
    to read instead of the terminal status every failure shares. A result the
    harness cannot type is not evidence, so it names nothing rather than
    guessing; the status alone still ends the wait.
    """
    try:
        result = EngineeringRunResult(**(run.get("result") or {}))
    except (TypeError, ValidationError):
        return None
    return result.failure_reason.value if result.failure_reason is not None else None


async def _wait_for_terminal_run(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    run: dict,
    *,
    deadline: float,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    story_alive: Callable[[], Awaitable[bool]],
) -> dict | None:
    """Read one Run until terminal without sleeping after its terminal result.

    Two fact sources: the Run itself, and the story that owns it. Both go through
    `resolve_wait_pass`, so the pass an expired deadline takes reads the story as
    well as the Run — a story parked while the repair Run is still `running` is
    a readable fact, and the previous shape reported it as a timeout. Returning
    `None` here means neither source answered; the caller names that deadline.
    """

    async def terminal_run_fact() -> WaitOutcome:
        nonlocal run
        if run["status"] not in TERMINAL_RUN_STATUSES:
            run = await _read_run(api_internal, run["id"])
        return observed(run) if run["status"] in TERMINAL_RUN_STATUSES else KEEP_WAITING

    while True:
        outcome = await resolve_wait_pass(
            run_fact=terminal_run_fact, story_alive=story_alive, on_poll=on_poll
        )
        if outcome.settled:
            return settle(ctx, outcome)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


def _record_attempt_exit(
    ctx: dict, repair_attempt: dict, *, recorded_before: str | None, deadline_reason: str
) -> None:
    """Copy the reason the inner wait recorded; synthesise one only if it left none.

    A `None` from an inner wait means that wait already said why it stopped, and
    the attempt record has to say the same thing the run-level evidence does — a
    story refusal recorded beside the repair as a timeout makes the artifact
    contradict itself about one event. Only a wait that genuinely ran out of
    clock with nothing to read leaves no reason, and only that one is a deadline.
    """
    error = ctx.get("settings_seed_repair_error")
    if error is None or error == recorded_before:
        error = deadline_reason
        ctx["settings_seed_repair_error"] = error
    repair_attempt["error"] = error


async def _follow_manifest_repair(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    result: DeployRunResult,
    *,
    repair_cap: int,
    repair_cap_label: str,
    overall_deadline: float,
    repair_budget: float,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    wait_followup: FollowupDeployWait,
) -> tuple[DeployRunResult | None, int | None]:
    """Await one scheduler-owned manifest repair and its fresh deploy."""
    attempt = result.deploy_fix_attempt + 1
    if attempt > repair_cap:
        ctx["settings_seed_repair_error"] = (
            f"manifest repair exceeded {repair_cap_label} {repair_cap}"
        )
        return None, repair_cap
    source = _settings_seed_source_created_at(ctx)
    if source is None:
        return None, repair_cap
    deadline = min(overall_deadline, time.monotonic() + repair_budget)
    alive = _story_alive_gate(api_internal, ctx, attempt, poll_interval=poll_interval)
    repair = await _wait_for_manifest_repair_run(
        api_internal,
        ctx,
        source_run_id=ctx["deploy_run_id"],
        attempt=attempt,
        deadline=deadline,
        poll_interval=poll_interval,
        on_poll=on_poll,
        story_alive=alive,
    )
    if repair is None:
        return None, repair_cap
    ctx.setdefault("settings_seed_repair_run_ids", []).append(repair["id"])
    ctx["settings_seed_repair_run_status"] = repair["status"]
    repair_attempt = {
        "attempt": attempt,
        "run_id": repair["id"],
        "status": repair["status"],
        "error": None,
    }
    ctx.setdefault("settings_seed_repair_attempts", []).append(repair_attempt)
    before = ctx.get("settings_seed_repair_error")
    repair = await _wait_for_terminal_run(
        api_internal,
        ctx,
        repair,
        deadline=deadline,
        poll_interval=poll_interval,
        on_poll=on_poll,
        story_alive=alive,
    )
    if repair is None:
        _record_attempt_exit(
            ctx,
            repair_attempt,
            recorded_before=before,
            deadline_reason=f"manifest repair attempt {attempt} timed out",
        )
        return None, repair_cap
    ctx["settings_seed_repair_run_status"] = repair["status"]
    repair_attempt["status"] = repair["status"]
    if repair["status"] != "completed":
        classification = _repair_failure_classification(repair)
        error = f"manifest repair Run {repair['id']} ended {repair['status']}"
        if classification is not None:
            error = f"{error}: {classification}"
        repair_attempt["error"] = error
        ctx["settings_seed_repair_error"] = error
        return None, repair_cap
    before = ctx.get("settings_seed_repair_error")
    result: DeployRunResult | None = await wait_followup(
        api_internal,
        ctx,
        deadline=deadline,
        created_after=source,
        poll_interval=poll_interval,
        on_poll=on_poll,
        story_alive=alive,
    )
    if result is None:
        _record_attempt_exit(
            ctx,
            repair_attempt,
            recorded_before=before,
            deadline_reason=(
                f"manifest repair attempt {attempt} follow-up deploy ended without a reason"
            ),
        )
    return result, repair_cap


async def _follow_convergent_retry(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    retries: int,
    retry_cap: int | None,
    overall_deadline: float,
    retry_budget: float,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    wait_followup: FollowupDeployWait,
) -> tuple[DeployRunResult | None, int, int | None]:
    """Await one scheduler-owned same-commit retry and preserve its cap."""
    if retry_cap is None:
        retry_cap = await _settings_seed_runtime_cap(
            api_internal, ctx, DEPLOY_MAX_RETRIES_CONFIG_KEY
        )
    if retry_cap is None:
        return None, retries, retry_cap
    # Local retries cap this wait; scheduler's persisted counter is story-wide.
    if retries + 1 >= retry_cap:
        ctx["settings_seed_repair_error"] = (
            f"settings-seed retry exceeded scheduler cap {retry_cap}"
        )
        return None, retries, retry_cap
    source = _settings_seed_source_created_at(ctx)
    if source is None:
        return None, retries, retry_cap
    retries += 1
    alive = _story_alive_gate(api_internal, ctx, None, poll_interval=poll_interval)
    result = await wait_followup(
        api_internal,
        ctx,
        deadline=min(overall_deadline, time.monotonic() + retry_budget),
        created_after=source,
        poll_interval=poll_interval,
        on_poll=on_poll,
        story_alive=alive,
    )
    return result, retries, retry_cap


async def follow_settings_seed(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    result: DeployRunResult,
    *,
    repair_budget: float,
    retry_budget: float,
    overall_budget: float,
    max_manifest_repairs: int | None = None,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    wait_followup: FollowupDeployWait,
) -> DeployRunResult | None:
    """Mirror scheduler seed routing within per-attempt and overall ceilings."""
    if max_manifest_repairs is not None and max_manifest_repairs <= 0:
        raise ValueError("max_manifest_repairs must be positive when set")
    retries = 0
    overall_deadline = time.monotonic() + overall_budget
    if time.monotonic() >= overall_deadline:
        ctx["settings_seed_repair_error"] = (
            "settings-seed follow-up exhausted its overall lifecycle deadline"
        )
        return None
    repair_cap: int | None = None
    repair_cap_label: str | None = None
    retry_cap: int | None = None
    while result.deploy_outcome is DeployOutcome.SETTINGS_SEED_FAILED:
        if time.monotonic() >= overall_deadline:
            ctx["settings_seed_repair_error"] = (
                "settings-seed follow-up exhausted its overall lifecycle deadline"
            )
            return None
        if result.settings_seed_needs_manifest_repair:
            if repair_cap is None:
                scheduler_repair_cap = await _settings_seed_runtime_cap(
                    api_internal, ctx, DEPLOY_MAX_FIX_ATTEMPTS_CONFIG_KEY
                )
                if scheduler_repair_cap is None:
                    return None
                repair_cap = min(scheduler_repair_cap, max_manifest_repairs or scheduler_repair_cap)
                repair_cap_label = (
                    "brief harness repair ceiling"
                    if max_manifest_repairs is not None
                    else "scheduler repair cap"
                )
            result, _ = await _follow_manifest_repair(
                api_internal,
                ctx,
                result,
                repair_cap=repair_cap,
                repair_cap_label=repair_cap_label or "scheduler repair cap",
                overall_deadline=overall_deadline,
                repair_budget=repair_budget,
                poll_interval=poll_interval,
                on_poll=on_poll,
                wait_followup=wait_followup,
            )
        elif result.settings_seed_can_converge:
            result, retries, retry_cap = await _follow_convergent_retry(
                api_internal,
                ctx,
                retries=retries,
                retry_cap=retry_cap,
                overall_deadline=overall_deadline,
                retry_budget=retry_budget,
                poll_interval=poll_interval,
                on_poll=on_poll,
                wait_followup=wait_followup,
            )
        else:
            return result
        if result is None:
            return None
    return result
