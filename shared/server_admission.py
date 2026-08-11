"""The single rule that decides whether a server may host a project application.

Two places used to answer that question independently and only from server
status:

- ``services/langgraph/src/allocations.py::_find_suitable_server`` — the allocator
  that actually picks the machine an application is placed on;
- ``services/scheduler/src/tasks/supervisor.py::_resources_available`` — the rule
  that lets a capacity-parked task leave the wait state.

Both now resolve through :func:`server_admission_rejection`, so "resources became
available" cannot mean something different from "this server may take an
application": a task can no longer wake up towards a target the allocator would
then refuse.

Admission is fail-closed. A server is a valid target only when it is managed, its
status is operational, its software provisioning is recorded complete, and it
carries no open provisioning-failure incident. A ``provisioning_phase`` that is
missing, empty or unknown counts as "not finished" — an unknown provisioning
state is not readiness.
"""

from collections.abc import Iterable
from enum import StrEnum

from shared.contracts.dto.incident import IncidentDTO, IncidentType
from shared.contracts.dto.server import ServerDTO, ServerStatus

# `provisioning_phase` lives in `servers.labels`. The infra-service provisioner is
# its only writer: `software_installation` when the software phase starts, and
# `complete` only when that phase succeeds — a failed phase leaves the earlier
# value in place. Admission is its only reader.
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


# Rejections that describe an unfinished or broken host build. They are an
# infrastructure situation, never a capacity shortage and never a product defect.
PROVISIONING_REJECTIONS: frozenset[ServerAdmissionRejection] = frozenset(
    {
        ServerAdmissionRejection.PROVISIONING_INCOMPLETE,
        ServerAdmissionRejection.PROVISIONING_FAILED,
    }
)


def provisioning_failed_server_handles(incidents: Iterable[IncidentDTO]) -> frozenset[str]:
    """Return the handles of servers with an open provisioning-failure incident.

    Feed this the active incidents (``GET /incidents/active``): the journal entry
    that provisioning recovery opens and ``server_sync`` closes again is the
    system's existing record of "this host's build is broken".
    """
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
    # An absent label is a state, not a missing setting: a server whose
    # provisioning never reached the completion write is not provisioned.
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
