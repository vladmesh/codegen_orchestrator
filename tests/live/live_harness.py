"""Safety contracts shared by Stage 7 live tests."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import time

import httpx


@asynccontextmanager
async def cleanup_guard(cleanup: Callable[[], Awaitable[None]]):
    """Always clean a live context and retain both body and cleanup failures."""
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc

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


async def run_non_llm_qa(
    client: httpx.AsyncClient,
    deployed_url: str,
    *,
    timeout: float,
    poll_interval: float = 3,
) -> dict[str, str]:
    """Run an observable health-only QA gate and require terminal ``passed``."""
    run_id = f"health-{int(time.time())}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for path in ("/health", "/v1/health"):
            try:
                response = await client.get(f"{deployed_url}{path}")
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                return {"run_id": run_id, "status": "completed", "qa_outcome": "passed"}
        await asyncio.sleep(poll_interval)
    raise AssertionError(
        f"non-LLM QA run {run_id} ended with status=failed outcome=failed after {timeout}s"
    )
