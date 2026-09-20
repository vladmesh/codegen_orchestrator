"""Safety contracts shared by Stage 7 live tests."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import secrets

import structlog

logger = structlog.get_logger()

# Set LIVE_NO_CLEANUP=1 to leave owned resources in place after a live run so a
# failed/timed-out pipeline can be inspected live. The manifest under
# .live-manifests/ still records them for later `make test-live-clean`.
LIVE_NO_CLEANUP_ENV = "LIVE_NO_CLEANUP"
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}

# ── The Telegram ids a run registers itself at ───────────────────────────
#
# A level-1 run walks the product's own registration door: a Telegram id nobody
# has used before, a promo code minted for it, and a registration that redeems
# the code. The id is drawn from a band the harness keeps to, so that two
# concurrent runs on one database do not collide and so that a run's own user is
# recognisable at a glance.
#
# The band is *not* ownership. Telegram chooses account ids, not this harness:
# a nine-digit id in this range is an ordinary Telegram account and may belong
# to a real customer. The only thing the harness genuinely writes is the
# username it registers under, so that — and not the band — is what a sweep may
# select on. See `run_user_sweep_predicate`.
RUN_USER_TELEGRAM_ID_MIN = 970_000_000
RUN_USER_TELEGRAM_ID_MAX = 970_999_999

#: The username `register_run_owner` registers its user under. Written by the
#: harness and by nothing else, which is what makes it a safe sweep predicate.
RUN_USER_USERNAME_PREFIX = "live_run_"


def new_run_telegram_id() -> int:
    """A Telegram id for this run's own user, inside the harness's range.

    Random rather than sequential: runs are concurrent and share one database,
    and nothing here may depend on having read the table first. A collision is
    not silently tolerated — the caller registers through the product's door and
    a taken id makes that registration refuse, which is a refusal naming its
    phase rather than a run quietly adopting somebody else's user.
    """
    return RUN_USER_TELEGRAM_ID_MIN + secrets.randbelow(
        RUN_USER_TELEGRAM_ID_MAX - RUN_USER_TELEGRAM_ID_MIN + 1
    )


def run_user_username(telegram_id: int) -> str:
    """The username this harness registers a run-owned user under."""
    return f"{RUN_USER_USERNAME_PREFIX}{telegram_id}"


def run_user_sweep_predicate() -> str:
    """The SQL that selects users this harness demonstrably registered itself.

    The stand sweep's second selection, next to its title prefixes. Both halves
    are needed and only the second is ownership: the id band keeps the harness's
    runs apart from each other, but Telegram hands out ids and a real account
    can sit anywhere in it, so the band alone would be a blind range delete. The
    username is written by `register_run_owner` and by nothing else in this
    system — the same kind of naming as a contour's project title prefix — so a
    row matching both is residue of this harness and a row matching only the
    band is left alone.

    Even so, the sweep applies this root only in a contour that owns live runs;
    production is swept exactly as it was before the registration door existed.
    """
    return (
        f"telegram_id BETWEEN {RUN_USER_TELEGRAM_ID_MIN} AND {RUN_USER_TELEGRAM_ID_MAX} "
        f"AND username LIKE '{RUN_USER_USERNAME_PREFIX}%'"
    )


def run_created_at(run: dict) -> datetime:
    """Parse the timestamp used to distinguish a newly created Run."""
    value = run.get("created_at")
    if not isinstance(value, str):
        raise ValueError(f"Run {run.get('id')} has no created_at timestamp")
    created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        raise ValueError(f"Run {run.get('id')} has a naive created_at timestamp")
    return created_at


def no_cleanup_enabled() -> bool:
    """True when LIVE_NO_CLEANUP asks teardown to leave owned resources in place."""
    return os.environ.get(LIVE_NO_CLEANUP_ENV) == "1"


def _log_cleanup_skipped(manifest: "OwnershipManifest") -> None:
    """Emit a visible warning listing the owned resources teardown left behind."""
    logger.warning(
        "cleanup skipped — resources left for debugging",
        env_flag=LIVE_NO_CLEANUP_ENV,
        run_id=manifest.run_id,
        manifest_file=f".live-manifests/{manifest.run_id}.json",
        left=[f"{resource.kind} {resource.identifier}" for resource in manifest.resources],
    )


@asynccontextmanager
async def cleanup_guard(
    cleanup: Callable[[], Awaitable[None]],
    *,
    manifest: "OwnershipManifest",
):
    """Always clean a live context and retain both body and cleanup failures.

    With LIVE_NO_CLEANUP set, teardown is skipped so owned resources stay live for
    debugging and a warning lists what remains. The run's primary error is still
    raised unchanged — the flag only affects teardown, never the test result.
    """
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc

    if no_cleanup_enabled():
        _log_cleanup_skipped(manifest)
        if primary_error is not None:
            raise primary_error
        return

    try:
        await cleanup()
    except BaseException as cleanup_error:
        if primary_error is not None:
            raise BaseExceptionGroup(
                "live run and owned-resource cleanup failed",
                [primary_error, cleanup_error],
            ) from None
        raise
    if primary_error is not None:
        raise primary_error


@asynccontextmanager
async def cleanup_on_error(cleanup: Callable[[], Awaitable[None]]):
    """Clean a partially created context only when its creation fails."""
    try:
        yield
    except BaseException as primary_error:
        try:
            await cleanup()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "owned-resource creation and cleanup failed",
                [primary_error, cleanup_error],
            ) from None
        raise


def resolve_repo_root(source: Path = Path(__file__)) -> Path:
    """Resolve a verified checkout root from an override or this module."""
    override = os.environ.get("ORCHESTRATOR_ROOT")
    root = Path(override).expanduser() if override else source.resolve().parents[2]
    root = root.resolve()
    if not (root / "pyproject.toml").is_file() or not (root / "tests" / "live").is_dir():
        raise RuntimeError(
            f"ORCHESTRATOR_ROOT must be a codegen_orchestrator checkout with "
            f"pyproject.toml and tests/live: {root}"
        )
    return root


@dataclass(frozen=True)
class OwnedResource:
    kind: str
    identifier: str
    metadata: dict = field(default_factory=dict)


class CleanupError(AssertionError):
    """One or more owned resources could not be removed or verified absent."""


@dataclass
class OwnershipManifest:
    """Resources created by one live run, in creation order."""

    run_id: str
    resources: list[OwnedResource] = field(default_factory=list)

    def own(self, kind: str, identifier: str, **metadata: object) -> None:
        """Record one owned resource, keyed by ``(kind, identifier)``.

        Owning the same resource again enriches the record in place instead of
        appending a second one: a resource can be written ahead of its creation
        with only the facts known then (a deploy is owned by stack name before
        any target runs it) and completed later with the facts the pipeline
        produced (server, port). Metadata already recorded survives an enrichment
        that does not mention it.
        """
        resource = OwnedResource(kind, str(identifier), dict(metadata))
        for index, existing in enumerate(self.resources):
            if existing.kind != resource.kind or existing.identifier != resource.identifier:
                continue
            merged = {**existing.metadata, **resource.metadata}
            if merged != existing.metadata:
                self.resources[index] = OwnedResource(resource.kind, resource.identifier, merged)
            return
        self.resources.append(resource)

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "resources": [
                        {"kind": item.kind, "identifier": item.identifier, **item.metadata}
                        for item in self.resources
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def teardown(
        self,
        *,
        delete: Callable[[OwnedResource], None],
        exists: Callable[[OwnedResource], bool],
    ) -> None:
        errors: list[str] = []
        for resource in reversed(self.resources):
            try:
                delete(resource)
            except Exception as exc:
                errors.append(f"{resource.kind} {resource.identifier}: {exc}")
            try:
                if exists(resource):
                    errors.append(f"{resource.kind} {resource.identifier} still exists")
            except Exception as exc:
                errors.append(
                    f"{resource.kind} {resource.identifier}: absence verification failed: {exc}"
                )
        if errors:
            raise CleanupError("owned-resource cleanup failed: " + "; ".join(errors))
