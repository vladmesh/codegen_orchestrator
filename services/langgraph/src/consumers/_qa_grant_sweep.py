"""The sweep that owns every SSH grant a QA run may still be holding.

The runner revokes in its `finally`, and that covers the ordinary end of a run.
It does not cover the run that was killed between writing the record and hearing
back from the install, or the one whose revoke connection died with it. In both
of those the only thing that knows a key may be on a target is the durable
record on the QA run (`QASshGrant`), and this is what reads it.

It reconciles from state, not from the happy path, like the temporary-access
sweep it is modelled on: every QA run carrying a record that is not `RELEASED`
gets a revoke attempt and a readback, however it got that way and however many
times it has already been tried. A revoke of a marker that was never installed
reads back zero and closes the record, so the ambiguous case costs one ssh and
resolves itself.

The selection is by the state of the record and by nothing else. It used to ask
for QA runs started inside a 24-hour window, and that was the wrong key: an
outage longer than the window left the record — and the `authorized_keys` line
it stands for — permanently outside the reach of the only process that removes
it. Age neither closes a record nor excuses skipping one; the only bound is how
many rows come back at once, and the sweep walks the pages until they run out.

While a record cannot be closed, the run says so. After
`GRANT_SWEEP_ESCALATE_AFTER` failed attempts the run's outcome is replaced with
a `qa_cleanup_failed` blocker naming the marker and the host, so residual access
reaches a human through the same channel every other QA blocker does instead of
living in this module's logs.

It runs in `qa-worker` rather than the scheduler because that is where the
target code, the SSH client and the fleet key already are; a second copy in the
scheduler would be a second implementation of the same revoke.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic import ValidationError
import structlog

from shared.contracts.dto.qa_ssh_grant import QA_SSH_GRANT_KEY, QASshGrant, QASshGrantState
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import (
    QABlocker,
    QABlockerCategory,
    QARunResult,
)
from shared.contracts.queues.qa import QAOutcome

from ..clients.api import api_client
from ._qa_target import revoke_grant

logger = structlog.get_logger(__name__)

GRANT_SWEEP_INTERVAL = 300  # 5 minutes
# How many unreleased records one request brings back. This bounds the response,
# not the work: a full page means there is another one, and the cycle keeps
# asking. Nothing is dropped for being past the end of a page.
GRANT_SWEEP_PAGE = 100
# Attempts before the residual access stops being a retry and becomes the run's
# reported outcome. Escalating does not close the record: it stays selected, and
# the sweep keeps trying to take the access back until a readback proves it gone.
GRANT_SWEEP_ESCALATE_AFTER = 3


def _open_grant(run_metadata: dict) -> QASshGrant | None:
    """The grant this run may still be holding, if it is holding one."""
    raw = (run_metadata or {}).get(QA_SSH_GRANT_KEY)
    if not raw:
        return None
    grant = QASshGrant.model_validate(raw)
    return grant if grant.held else None


async def _record(run_id: str, grant: QASshGrant) -> None:
    await api_client.patch(
        f"runs/{run_id}",
        json={"run_metadata": {QA_SSH_GRANT_KEY: grant.model_dump(mode="json")}},
    )


async def _report_residual_access(run_id: str, grant: QASshGrant) -> bool:
    """Put the residual access on the run, unless the run already said something else.

    A run that recorded its own outcome first keeps it — the QA worker and this
    sweep can be deciding about the same run at the same moment, and the API
    keeps whichever landed. Refused here is information, not failure: the access
    is still out and the next cycle still tries to take it back.
    """
    result = QARunResult(
        qa_outcome=QAOutcome.BLOCKED,
        summary="QA left access on the target that could not be proven gone",
        blocker=QABlocker(
            category=QABlockerCategory.QA_CLEANUP_FAILED,
            attempted="remove the QA run's one-shot key from the target",
            sent=f"authorized_keys entry {grant.marker} on {grant.server_ip}",
            received=grant.detail or "the target could not be read back",
        ),
        state_changes=[
            {
                "resource": f"authorized_keys entry {grant.marker} on {grant.server_ip}",
                "operation": "created",
                "cleanup": {
                    "attempted": True,
                    "succeeded": False,
                    "detail": grant.detail or "the target could not be read back",
                },
            }
        ],
    )
    try:
        await api_client.patch(
            f"runs/{run_id}",
            json={
                "status": RunStatus.COMPLETED.value,
                "result": result.model_dump(mode="json"),
            },
        )
    except httpx.HTTPStatusError as error:
        if error.response.status_code != httpx.codes.CONFLICT:
            raise
        logger.warning("qa_grant_residual_run_already_settled", run_id=run_id)
        return False
    return True


async def _reconcile(run_id: str, grant: QASshGrant) -> str:
    """Take one grant back and read the target to see whether it went."""
    log = logger.bind(run_id=run_id, marker=grant.marker, server_ip=grant.server_ip)
    ssh_key = await api_client.get_server_ssh_key(grant.server_handle)
    if not ssh_key:
        detail = f"no server key for {grant.server_handle}; the grant cannot be taken back"
        log.error("qa_grant_sweep_no_server_key")
        await _record(
            run_id,
            grant.model_copy(
                update={"revoke_attempts": grant.revoke_attempts + 1, "detail": detail}
            ),
        )
        return "failed"

    try:
        residual = await revoke_grant(
            server_ip=grant.server_ip,
            ssh_user=grant.ssh_user,
            fleet_key=ssh_key,
            marker=grant.marker,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is a retry, not a crash
        residual = f"revocation failed: {exc}"

    if residual is None:
        await _record(run_id, grant.model_copy(update={"state": QASshGrantState.RELEASED}))
        log.info("qa_grant_sweep_revoked")
        return "revoked"

    attempts = grant.revoke_attempts + 1
    failed = grant.model_copy(update={"revoke_attempts": attempts, "detail": residual})
    await _record(run_id, failed)
    log.warning("qa_grant_sweep_revoke_failed", attempts=attempts, residual=residual)
    if attempts >= GRANT_SWEEP_ESCALATE_AFTER:
        await _report_residual_access(run_id, failed)
        return "escalated"
    return "failed"


async def sweep_qa_ssh_grants() -> dict[str, int]:
    """Drive every unreleased grant towards removal, however old its run is.

    Pages are walked to the end, oldest record first. A record the API returns
    but that has meanwhile been released is simply skipped; a record missed
    because the page shifted under a release is picked up by the next cycle,
    because it is still selected while it is still open.
    """
    counts = {"seen": 0, "revoked": 0, "failed": 0, "escalated": 0, "unreadable": 0}
    offset = 0
    while True:
        runs = await api_client.list_runs_holding_qa_ssh_grant(
            limit=GRANT_SWEEP_PAGE, offset=offset
        )
        for run in runs:
            try:
                grant = _open_grant(run.run_metadata)
            except ValidationError as error:
                # A record no version of the schema can read is still a key on a
                # target, so it stays selected. What it must not do is end the
                # cycle: everything behind it would stop being reached at all,
                # which is the failure this selection exists to prevent.
                counts["unreadable"] += 1
                logger.error("qa_grant_sweep_unreadable_record", run_id=run.id, error=str(error))
                continue
            if grant is None:
                continue
            counts["seen"] += 1
            counts[await _reconcile(run.id, grant)] += 1
        if len(runs) < GRANT_SWEEP_PAGE:
            break
        offset += len(runs)
    if counts["seen"] or counts["unreadable"]:
        logger.info("qa_grant_sweep_cycle", **counts)
    return counts


async def qa_grant_sweep_loop() -> None:
    """Run the sweep forever; a failing cycle is retried, never fatal."""
    logger.info("qa_grant_sweep_started", interval_s=GRANT_SWEEP_INTERVAL)
    while True:
        try:
            await sweep_qa_ssh_grants()
        except Exception:
            logger.exception("qa_grant_sweep_cycle_error")
        await asyncio.sleep(GRANT_SWEEP_INTERVAL)
