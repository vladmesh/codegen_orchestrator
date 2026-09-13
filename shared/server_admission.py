"""Fail-closed admission of managed, operational, fully provisioned, proved servers."""

from collections.abc import Iterable
from enum import StrEnum

from shared.contracts.dto.incident import IncidentDTO, IncidentType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.qa_target_profile import QATargetReceiptRejection, qa_target_receipt_rejection

# Provisioning completion is the only admitting label value.
PROVISIONING_PHASE_LABEL = "provisioning_phase"
PROVISIONING_PHASE_SOFTWARE_INSTALLATION = "software_installation"
PROVISIONING_PHASE_COMPLETE = "complete"

ADMITTING_SERVER_STATUSES: frozenset[ServerStatus] = frozenset(
    {ServerStatus.ACTIVE, ServerStatus.READY, ServerStatus.IN_USE}
)

#: The status managed-target reconciliation moves a server to when its stored
#: key does not parse, its administrative account cannot log in, or its QA role
#: cannot be applied and proved. Nothing schedules provisioning from it and the
#: health checker does not probe it back to an operational status.
TARGET_NOT_READY_STATUS = ServerStatus.ERROR


#: Where managed-target reconciliation may act: operational rows and the rows it
#: parked itself. A row still being provisioned belongs to the provisioner.
RECONCILABLE_TARGET_STATUSES: frozenset[ServerStatus] = ADMITTING_SERVER_STATUSES | {
    TARGET_NOT_READY_STATUS
}


def target_readiness_reconcilable(server: ServerDTO) -> bool:
    """Whether non-destructive readiness reconciliation may act on this row.

    Explicit management and a finished software phase are the whole authority:
    no provider id and no destructive-operation allowlist entry is needed or
    granted, because reconciliation never reinstalls, never replaces a firewall
    and never removes anything the QA role did not create.
    """
    return (
        server.is_managed
        and server.status in RECONCILABLE_TARGET_STATUSES
        and server.labels.get(PROVISIONING_PHASE_LABEL) == PROVISIONING_PHASE_COMPLETE
    )


class ServerAdmissionRejection(StrEnum):
    """Why a server may not receive a project application."""

    NOT_MANAGED = "not_managed"
    # Reconciliation found failed key, login or role evidence on this target.
    TARGET_NOT_READY = "target_not_ready"
    STATUS_NOT_ADMITTING = "status_not_admitting"
    PROVISIONING_INCOMPLETE = "provisioning_incomplete"
    PROVISIONING_FAILED = "provisioning_failed"
    QA_TARGET_RECEIPT_MISSING = QATargetReceiptRejection.MISSING.value
    QA_TARGET_RECEIPT_STALE = QATargetReceiptRejection.STALE.value


#: Every admission rejection is platform state, never a project capacity claim.
ADMISSION_FAILURE_REASON: AllocationFailureReason = AllocationFailureReason.SERVER_NOT_PROVISIONED


def provisioning_failed_server_handles(incidents: Iterable[IncidentDTO]) -> frozenset[str]:
    """Return servers carrying an open provisioning-failure incident."""
    return frozenset(
        incident.server_handle
        for incident in incidents
        if incident.incident_type is IncidentType.PROVISIONING_FAILED
        and incident.server_handle is not None
    )


def server_admission_rejection(
    server: ServerDTO, provisioning_failed_handles: frozenset[str]
) -> ServerAdmissionRejection | None:
    """Return why this server cannot host an application, or ``None`` if it can."""
    if not server.is_managed:
        return ServerAdmissionRejection.NOT_MANAGED
    if server.status is TARGET_NOT_READY_STATUS:
        return ServerAdmissionRejection.TARGET_NOT_READY
    if server.status not in ADMITTING_SERVER_STATUSES:
        return ServerAdmissionRejection.STATUS_NOT_ADMITTING
    # Missing or unknown provisioning state is not admission.
    if server.labels.get(PROVISIONING_PHASE_LABEL) != PROVISIONING_PHASE_COMPLETE:
        return ServerAdmissionRejection.PROVISIONING_INCOMPLETE
    if server.handle in provisioning_failed_handles:
        return ServerAdmissionRejection.PROVISIONING_FAILED
    # A label saying the account exists is not proof the current QA artefacts do.
    receipt = qa_target_receipt_rejection(server)
    if receipt is not None:
        return ServerAdmissionRejection(receipt.value)
    return None


def server_admits_application(
    server: ServerDTO, provisioning_failed_handles: frozenset[str]
) -> bool:
    """Return whether this server may host a project application at all."""
    return server_admission_rejection(server, provisioning_failed_handles) is None
