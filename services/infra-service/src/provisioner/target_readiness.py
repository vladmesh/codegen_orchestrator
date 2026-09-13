"""Reconcile every managed deploy target to the current QA target profile.

    docker compose exec -T infra-service \\
        python -m src.provisioner.target_readiness --revision "$DEPLOYED_SHA"

The production deploy runs this once, after the new services are healthy. Every
managed row gets exactly one outcome:

* a row whose software phase is complete, in any status provisioning does not
  own — `ready`, `error`, `unreachable`, `reserved` and the rest — goes through
  `retrofit_qa_identity`: key parse, login, privilege path, role and proof. Its
  verdict is `ready` or `not_ready`, recorded by the API, which leaves any
  lifecycle state it did not itself park untouched.
* a row provisioning currently owns (`pending_setup`, `provisioning`,
  `force_rebuild`) is `in_progress`, and a managed row whose software phase is
  not complete is `unhandled`. Neither is reconciled, and neither is a success.
* a verdict the API refused because the row's identity changed meanwhile is
  `superseded`; one that could not be recorded at all is `unrecorded`.

It never reinstalls a server, never replaces a firewall and never starts QA or
a stand run: the only playbooks it can run are the two preflight probes and the
retrofit.

Exit status: 0 only when every managed row has a recorded `ready` or `not_ready`
verdict. A target found not ready is a recorded fact admission already enforces.
Any other outcome exits 1, because then some managed target was not reconciled
by the revision being deployed and the deploy must not report success over it.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import re
import sys

import structlog

from shared.contracts.dto.server import ServerDTO
from shared.server_admission import IN_PROGRESS_TARGET_STATUSES, target_readiness_reconcilable

from .ansible_runner import AnsibleRunner
from .api_client import TargetReadinessSupersededError, list_managed_servers
from .operations import NO_PUBLIC_ADDRESS, NOT_RECONCILABLE, retrofit_qa_identity

logger = structlog.get_logger()

_REVISION = re.compile(r"^[0-9a-f]{40}$")

#: The only outcomes a successful deploy may carry.
RECORDED_OUTCOMES = frozenset({"ready", "not_ready"})


@dataclass(frozen=True)
class TargetVerdict:
    """What reconciliation did for one managed server."""

    server_handle: str
    # ready | not_ready | in_progress | unhandled | superseded | unrecorded
    outcome: str
    message: str


async def _reconcile_one(
    server: ServerDTO, ansible_runner: AnsibleRunner, *, revision: str
) -> TargetVerdict:
    if server.status in IN_PROGRESS_TARGET_STATUSES:
        return TargetVerdict(
            server.handle,
            "in_progress",
            f"provisioning owns this row ({server.status.value}); it was not reconciled",
        )
    if not target_readiness_reconcilable(server):
        return TargetVerdict(
            server.handle,
            "unhandled",
            "a managed row whose software phase is not complete; it was not reconciled",
        )
    try:
        ready, message = await retrofit_qa_identity(
            server.handle, ansible_runner, revision=revision
        )
    except TargetReadinessSupersededError as exc:
        return TargetVerdict(server.handle, "superseded", str(exc))
    except Exception as exc:  # noqa: BLE001 — one target's lost verdict must not hide the rest
        logger.error(
            "target_readiness_unrecorded",
            server_handle=server.handle,
            error_type=type(exc).__name__,
        )
        return TargetVerdict(server.handle, "unrecorded", type(exc).__name__)
    if not ready and message in (NOT_RECONCILABLE, NO_PUBLIC_ADDRESS):
        # The row changed between listing and reconciliation, or cannot be
        # addressed at all: no verdict was recorded for it.
        return TargetVerdict(server.handle, "unhandled", message)
    return TargetVerdict(server.handle, "ready" if ready else "not_ready", message)


async def reconcile_managed_targets(
    ansible_runner: AnsibleRunner, *, revision: str
) -> list[TargetVerdict]:
    """Give every managed server exactly one outcome, one at a time."""
    return [
        await _reconcile_one(server, ansible_runner, revision=revision)
        for server in sorted(await list_managed_servers(), key=lambda row: row.handle)
    ]


def _revision(value: str) -> str:
    if not _REVISION.fullmatch(value):
        raise argparse.ArgumentTypeError("--revision must be a full 40-character commit SHA")
    return value


async def main(argv: list[str]) -> int:
    """Reconcile the fleet, print one JSON line per target, and return the exit status."""
    parser = argparse.ArgumentParser(prog="python -m src.provisioner.target_readiness")
    parser.add_argument("--revision", required=True, type=_revision)
    args = parser.parse_args(argv)
    verdicts = await reconcile_managed_targets(AnsibleRunner(), revision=args.revision)
    for verdict in verdicts:
        print(json.dumps(asdict(verdict)))  # noqa: T201 — the deploy step reads stdout
    return 0 if all(verdict.outcome in RECORDED_OUTCOMES for verdict in verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
