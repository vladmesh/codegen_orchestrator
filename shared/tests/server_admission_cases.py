"""The server-admission state matrix, shared by every test of the placement rule.

One table, three call sites:

- the predicate itself (`shared/tests/unit/test_server_admission.py`);
- the allocator that picks the host (`services/langgraph/tests/unit/test_allocator.py`);
- the scheduler's resource wait (`services/scheduler/tests/unit/test_supervisor.py`).

All three assert the same expected verdict for the same states. If one admission
path stops going through `shared.server_admission` and grows its own rule again,
its copy of this table starts disagreeing with the others and that suite fails —
which is exactly the divergence the two paths must not have.

Every case is built with generous capacity and fresh metrics, so capacity and
metric freshness never decide the verdict: only the admission rule does.
"""

from dataclasses import dataclass, field
from datetime import datetime

from shared.contracts.dto.incident import IncidentDTO, IncidentStatus, IncidentType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.server_admission import (
    PROVISIONING_PHASE_COMPLETE,
    PROVISIONING_PHASE_LABEL,
    PROVISIONING_PHASE_SOFTWARE_INSTALLATION,
)

ADMISSION_CASE_HANDLE = "srv-admission"
ADMISSION_CASE_CAPACITY_RAM_MB = 4096
ADMISSION_CASE_CAPACITY_DISK_MB = 50000


@dataclass(frozen=True)
class AdmissionCase:
    """One server state and the verdict every admission path owes it."""

    name: str
    status: ServerStatus
    labels: dict = field(default_factory=dict)
    provisioning_failed: bool = False
    is_managed: bool = True
    admitted: bool = False


ADMISSION_CASES: tuple[AdmissionCase, ...] = (
    AdmissionCase(
        name="provisioned_active_server",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        admitted=True,
    ),
    AdmissionCase(
        name="provisioned_ready_server",
        status=ServerStatus.READY,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        admitted=True,
    ),
    AdmissionCase(
        name="provisioned_in_use_server",
        status=ServerStatus.IN_USE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        admitted=True,
    ),
    # The state this rule exists for: server_sync marks a managed host ACTIVE
    # before the software phase has written anything.
    AdmissionCase(name="active_without_provisioning_phase", status=ServerStatus.ACTIVE),
    AdmissionCase(
        name="active_while_installing_software",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_SOFTWARE_INSTALLATION},
    ),
    AdmissionCase(
        name="active_with_empty_provisioning_phase",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: ""},
    ),
    AdmissionCase(
        name="active_with_unknown_provisioning_phase",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: "hardening"},
    ),
    AdmissionCase(
        name="complete_but_provisioning_failed",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        provisioning_failed=True,
    ),
    AdmissionCase(
        name="still_provisioning_status",
        status=ServerStatus.PROVISIONING,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
    ),
    AdmissionCase(
        name="unreachable_status",
        status=ServerStatus.UNREACHABLE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
    ),
    AdmissionCase(
        name="unmanaged_server",
        status=ServerStatus.ACTIVE,
        labels={PROVISIONING_PHASE_LABEL: PROVISIONING_PHASE_COMPLETE},
        is_managed=False,
    ),
)


#: Every state in which admission refuses. Kept next to the table so a new
#: refusing state joins every "this is not a capacity shortage" assertion at
#: once, instead of being listed by hand in one suite and forgotten in another.
REFUSED_ADMISSION_CASES: tuple[AdmissionCase, ...] = tuple(
    case for case in ADMISSION_CASES if not case.admitted
)

#: The reasons that say the request was larger than the platform's memory or
#: hardware. No admission refusal may be reported as one of them: it describes a
#: host that may not take work, not a request that did not fit.
CAPACITY_REASONS: frozenset[AllocationFailureReason] = frozenset(
    {
        AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
        AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY,
        AllocationFailureReason.IMPOSSIBLE_CAPACITY,
    }
)


def admission_case_server(case: AdmissionCase, *, last_health_check: datetime) -> ServerDTO:
    """Build the server this case describes, roomy and freshly measured."""
    return ServerDTO(
        handle=ADMISSION_CASE_HANDLE,
        host=f"{ADMISSION_CASE_HANDLE}.example.com",
        public_ip="1.2.3.4",
        ssh_user="dev",
        status=case.status,
        is_managed=case.is_managed,
        labels=dict(case.labels),
        capacity_ram_mb=ADMISSION_CASE_CAPACITY_RAM_MB,
        capacity_disk_mb=ADMISSION_CASE_CAPACITY_DISK_MB,
        used_ram_mb=0,
        last_health_check=last_health_check,
        created_at=last_health_check,
        updated_at=last_health_check,
    )


def admission_case_incidents(case: AdmissionCase, *, detected_at: datetime) -> list[IncidentDTO]:
    """The active-incident journal this case implies."""
    if not case.provisioning_failed:
        return []
    return [
        IncidentDTO(
            id=1,
            server_handle=ADMISSION_CASE_HANDLE,
            incident_type=IncidentType.PROVISIONING_FAILED,
            status=IncidentStatus.DETECTED,
            detected_at=detected_at,
            created_at=detected_at,
            updated_at=detected_at,
        )
    ]
