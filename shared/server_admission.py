"""Fail-closed admission of managed, operational, fully provisioned, proved servers."""

from collections.abc import Iterable, Mapping
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

#: The status a readiness failure moves an admitting row to. The readiness
#: endpoint remembers the status it moved the row out of and restores it only
#: while that park still owns the row; nothing schedules provisioning from it.
TARGET_NOT_READY_STATUS = ServerStatus.ERROR

#: Statuses in which provisioning owns a managed row. Readiness reconciliation
#: does not act on them: the software play proves the profile when it completes.
IN_PROGRESS_TARGET_STATUSES: frozenset[ServerStatus] = frozenset(
    {ServerStatus.PENDING_SETUP, ServerStatus.PROVISIONING, ServerStatus.FORCE_REBUILD}
)

#: Managed rows that may still be without an administrative key: the ones whose
#: key provisioning mints. Provider discovery creates managed rows in
#: `pending_setup` and adopts inventory rows as `reserved`, and the provisioner
#: saves the key it generates only after that. Every other managed row needs one.
PROVISIONER_KEYED_STATUSES: frozenset[ServerStatus] = IN_PROGRESS_TARGET_STATUSES | {
    ServerStatus.RESERVED
}


def provisioning_phase_complete(labels: Mapping | None) -> bool:
    """Whether the labels record a finished software phase."""
    return (labels or {}).get(PROVISIONING_PHASE_LABEL) == PROVISIONING_PHASE_COMPLETE


def managed_row_requires_admin_key(
    *, is_managed: bool, status: str, labels: Mapping | None
) -> bool:
    """Whether a row in this state must hold a parseable administrative key.

    A managed row needs one unless provisioning still owns it and will mint it:
    a pre-provisioning status whose software phase is not complete.
    """
    if not is_managed:
        return False
    keyed_by_provisioning = str(status) in {state.value for state in PROVISIONER_KEYED_STATUSES}
    return not keyed_by_provisioning or provisioning_phase_complete(labels)


def target_readiness_reconcilable(server: ServerDTO) -> bool:
    """Whether non-destructive readiness reconciliation acts on this row.

    Every managed row whose software phase is complete, in any status provisioning
    does not currently own. Explicit management and that finished phase are the
    whole authority: no provider id and no destructive-operation allowlist entry
    is needed or granted, because reconciliation never reinstalls, never replaces
    a firewall and never removes anything the QA role did not create.
    """
    return (
        server.is_managed
        and server.status not in IN_PROGRESS_TARGET_STATUSES
        and provisioning_phase_complete(server.labels)
    )


class ServerAdmissionRejection(StrEnum):
    """Why a server may not receive a project application."""

    NOT_MANAGED = "not_managed"
    # Reconciliation recorded an unrepaired readiness failure on this target.
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
    if server.target_readiness_failure_phase is not None:
        return ServerAdmissionRejection.TARGET_NOT_READY
    if server.status not in ADMITTING_SERVER_STATUSES:
        return ServerAdmissionRejection.STATUS_NOT_ADMITTING
    # Missing or unknown provisioning state is not admission.
    if not provisioning_phase_complete(server.labels):
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
