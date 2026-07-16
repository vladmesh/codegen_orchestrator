"""Safety contracts shared by Stage 7 live tests."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Set LIVE_NO_CLEANUP=1 to leave owned resources in place after a live run so a
# failed/timed-out pipeline can be inspected live. The manifest under
# .live-manifests/ still records them for later `make test-live-clean`.
LIVE_NO_CLEANUP_ENV = "LIVE_NO_CLEANUP"


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
        resource = OwnedResource(kind, str(identifier), dict(metadata))
        if resource not in self.resources:
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
