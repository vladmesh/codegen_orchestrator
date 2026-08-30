"""Fail-closed admission of managed, operational, fully provisioned servers."""

from collections.abc import Iterable
from enum import StrEnum

from shared.contracts.dto.incident import IncidentDTO, IncidentType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.server import ServerDTO, ServerStatus

# Provisioning completion is the only admitting label value.
PROVISIONING_PHASE_LABEL = "provisioning_phase"
PROVISIONING_PHASE_SOFTWARE_INSTALLATION = "software_installation"
PROVISIONING_PHASE_COMPLETE = "complete"

ADMITTING_SERVER_STATUSES: frozenset[ServerStatus] = frozenset(
    {ServerStatus.ACTIVE, ServerStatus.READY, ServerStatus.IN_USE}
)


class ServerAdmissionRejection(StrEnum):
    """Why a server may not receive a project application."""

    NOT_MANAGED = "not_managed"
    STATUS_NOT_ADMITTING = "status_not_admitting"
    PROVISIONING_INCOMPLETE = "provisioning_incomplete"
    PROVISIONING_FAILED = "provisioning_failed"


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
    if server.status not in ADMITTING_SERVER_STATUSES:
        return ServerAdmissionRejection.STATUS_NOT_ADMITTING
    # Missing or unknown provisioning state is not admission.
    if server.labels.get(PROVISIONING_PHASE_LABEL) != PROVISIONING_PHASE_COMPLETE:
        return ServerAdmissionRejection.PROVISIONING_INCOMPLETE
    if server.handle in provisioning_failed_handles:
        return ServerAdmissionRejection.PROVISIONING_FAILED
    return None


def server_admits_application(
    server: ServerDTO, provisioning_failed_handles: frozenset[str]
) -> bool:
    """Return whether this server may host a project application at all."""
    return server_admission_rejection(server, provisioning_failed_handles) is None
