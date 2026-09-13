"""Reconcile every managed deploy target to the current QA target profile.

    docker compose exec -T infra-service \\
        python -m src.provisioner.target_readiness --revision "$DEPLOYED_SHA"

The production deploy runs this once, after the new services are healthy. Each
managed, provisioned target goes through `retrofit_qa_identity`: its stored key
is parsed, its administrative login and privilege path are proved, the current
`qa_identity` role is applied and proved, and the verdict — a receipt, or a
typed incident with the target taken out of admission — is recorded by the API.

It never reinstalls a server, never replaces a firewall and never starts QA or
a stand run: the only playbooks it can run are the preflight and the retrofit.

Exit status: 0 when every eligible target has a recorded verdict, ready or not.
A target found not ready is a recorded fact that admission already enforces, so
it is reported, not raised. 1 when any verdict could not be recorded — the API
did not answer or the journal write failed — because then the fleet's state is
unknown and the deploy must not report success over it.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import re
import sys

import structlog

from shared.server_admission import target_readiness_reconcilable

from .ansible_runner import AnsibleRunner
from .api_client import list_managed_servers
from .operations import retrofit_qa_identity

logger = structlog.get_logger()

_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class TargetVerdict:
    """What reconciliation recorded for one managed server."""

    server_handle: str
    outcome: str  # "ready" | "not_ready" | "skipped" | "unrecorded"
    message: str


async def reconcile_managed_targets(
    ansible_runner: AnsibleRunner, *, revision: str
) -> list[TargetVerdict]:
    """Run the readiness reconciliation over every managed server, one at a time."""
    verdicts: list[TargetVerdict] = []
    for server in sorted(await list_managed_servers(), key=lambda row: row.handle):
        if not target_readiness_reconcilable(server):
            verdicts.append(
                TargetVerdict(
                    server.handle,
                    "skipped",
                    f"status {server.status.value} or provisioning not complete",
                )
            )
            continue
        try:
            ready, message = await retrofit_qa_identity(
                server.handle, ansible_runner, revision=revision
            )
        except Exception as exc:  # noqa: BLE001 — one target's lost verdict must not hide the rest
            logger.error(
                "target_readiness_unrecorded",
                server_handle=server.handle,
                error_type=type(exc).__name__,
            )
            verdicts.append(TargetVerdict(server.handle, "unrecorded", type(exc).__name__))
            continue
        verdicts.append(TargetVerdict(server.handle, "ready" if ready else "not_ready", message))
    return verdicts


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
    return 1 if any(verdict.outcome == "unrecorded" for verdict in verdicts) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
