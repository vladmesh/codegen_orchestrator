"""Bounded settings-seed follow-up policy for the live Product Brief harness.

Only the deploy-result wait is injected by ``pipeline_helpers`` because it
combines deploy-Run discovery and outcome evidence collection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
import time
from typing import Protocol

import httpx
from live_harness import TERMINAL_RUN_STATUSES, run_created_at

from shared.contracts.dto.run_result import DeployRunResult, deploy_fix_run_id
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.deploy import DeployOutcome

DEPLOY_MAX_FIX_ATTEMPTS_CONFIG_KEY = "deploy.max_deploy_fix_attempts"
DEPLOY_MAX_RETRIES_CONFIG_KEY = "deploy.max_deploy_retries"
SETTINGS_SEED_STORY_POLL_INTERVAL = 30


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
    api_internal: httpx.AsyncClient, ctx: dict, attempt: int | None
) -> Callable[[], Awaitable[bool]]:
    """Return one cached-cadence check shared by every wait in one follow-up."""
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
        next_poll = time.monotonic() + SETTINGS_SEED_STORY_POLL_INTERVAL
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
    """Wait for the scheduler-owned repair Run or its terminal story refusal."""
    story_id = ctx["story_id"]
    repair_run_id = deploy_fix_run_id(source_run_id, attempt)
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        if not await story_alive():
            return None
        response = await api_internal.get(f"/api/runs/{repair_run_id}")
        if response.status_code == 404:
            await asyncio.sleep(poll_interval)
            continue
        response.raise_for_status()
        run = response.json()
        if (
            run.get("story_id") != story_id
            or (run.get("run_metadata") or {}).get("deploy_fix_attempt") != attempt
        ):
            ctx["settings_seed_repair_error"] = (
                f"manifest repair Run {repair_run_id} does not name story {story_id} "
                f"attempt {attempt}"
            )
            return None
        return run
    ctx["settings_seed_repair_error"] = (
        f"no manifest repair attempt {attempt} appeared for story {story_id} "
        "before the repair deadline"
    )
    return None


async def _wait_for_terminal_run(
    api_internal: httpx.AsyncClient,
    run: dict,
    *,
    deadline: float,
    poll_interval: float,
    on_poll: Callable[[], None] | None,
    story_alive: Callable[[], Awaitable[bool]],
) -> dict | None:
    """Read one Run until terminal without sleeping after its terminal result."""
    while run["status"] not in TERMINAL_RUN_STATUSES:
        if time.monotonic() >= deadline:
            return None
        if on_poll is not None:
            on_poll()
        if not await story_alive():
            return None
        await asyncio.sleep(poll_interval)
        response = await api_internal.get(f"/api/runs/{run['id']}")
        response.raise_for_status()
        run = response.json()
    return run


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
    alive = _story_alive_gate(api_internal, ctx, attempt)
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
    repair = await _wait_for_terminal_run(
        api_internal,
        repair,
        deadline=deadline,
        poll_interval=poll_interval,
        on_poll=on_poll,
        story_alive=alive,
    )
    if repair is None:
        error = f"manifest repair attempt {attempt} timed out"
        repair_attempt["error"] = error
        ctx.setdefault("settings_seed_repair_error", error)
        return None, repair_cap
    ctx["settings_seed_repair_run_status"] = repair["status"]
    repair_attempt["status"] = repair["status"]
    if repair["status"] != "completed":
        error = f"manifest repair Run {repair['id']} ended {repair['status']}"
        repair_attempt["error"] = error
        ctx["settings_seed_repair_error"] = error
        return None, repair_cap
    return (
        await wait_followup(
            api_internal,
            ctx,
            deadline=deadline,
            created_after=source,
            poll_interval=poll_interval,
            on_poll=on_poll,
            story_alive=alive,
        ),
        repair_cap,
    )


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
    alive = _story_alive_gate(api_internal, ctx, None)
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
