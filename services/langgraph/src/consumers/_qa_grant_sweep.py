"""Reconcile every unreleased QA SSH grant until readback proves removal."""

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
# Pagination bounds each response, not the set reconciled in a cycle.
GRANT_SWEEP_PAGE = 100
# Escalation reports residual access but does not stop reconciliation.
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
    """Report residual access without replacing an existing run outcome."""
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
            qa_ssh_user=grant.qa_ssh_user,
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
    """Reconcile all pages using a stable cursor as records leave the selection."""
    counts = {"seen": 0, "revoked": 0, "failed": 0, "escalated": 0, "unreadable": 0}
    after = None
    while True:
        runs = await api_client.list_runs_holding_qa_ssh_grant(limit=GRANT_SWEEP_PAGE, after=after)
        if runs:
            after = runs[-1]
        for run in runs:
            try:
                grant = _open_grant(run.run_metadata)
            except ValidationError as error:
                # An unreadable record must not block later grants in this cycle.
                counts["unreadable"] += 1
                logger.error("qa_grant_sweep_unreadable_record", run_id=run.id, error=str(error))
                continue
            if grant is None:
                continue
            counts["seen"] += 1
            counts[await _reconcile(run.id, grant)] += 1
        if len(runs) < GRANT_SWEEP_PAGE:
            break
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
