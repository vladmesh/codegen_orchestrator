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

How a refusal is *reported* lives here too, in :data:`ADMISSION_FAILURE_REASON`,
because both placement paths have to report it the same way.
"""

from collections.abc import Iterable
from enum import StrEnum

from shared.contracts.dto.incident import IncidentDTO, IncidentType
from shared.contracts.dto.run_result import AllocationFailureReason
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


#: The one allocation reason any admission rejection is reported as.
#:
#: There is a single value rather than a rejection-to-reason table because the
#: mapping has no branch to make: every member of `ServerAdmissionRejection` is a
#: statement about the platform's own host, and none of them is evidence that the
#: request was too large. Two of the four — the host is not managed, or its status
#: does not admit — are not literally an unfinished build, and the reason
#: vocabulary has no member for them; they are still platform state rather than
#: the project's, and the alternative to reusing the closest infrastructure reason
#: is describing them to the owner as a memory shortage, which is worse and false.
#:
#: Both placement paths in `services/langgraph/src/allocations.py` — the search
#: for a new host and the re-admission of a bound one — raise this constant, so
#: neither can report a rejection the other reports differently. A subset that
#: only some rejections belonged to was exactly that divergence: a server merely
#: in status `provisioning` fell out of it in the search path and left the
#: refusal to be described as `insufficient_free_memory`.
#:
#: The search path asks one question before this one, which the bound path has no
#: way to ask: whether any managed server could fit the request at all. That is a
#: fact about the fleet rather than about a host's state, and no wait can resolve
#: it, so it is reported as `IMPOSSIBLE_CAPACITY` and reaches an operator at once.
#: The order and its reason are stated at the check in `allocations.py`.
ADMISSION_FAILURE_REASON: AllocationFailureReason = AllocationFailureReason.SERVER_NOT_PROVISIONED


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
