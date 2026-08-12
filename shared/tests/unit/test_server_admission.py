"""The admission rule itself: which servers may host a project application."""

from datetime import UTC, datetime

import pytest

from shared.allocation_disposition import (
    ALLOCATION_DISPOSITIONS,
    AttemptDisposition,
)
from shared.contracts.dto.incident import IncidentDTO, IncidentStatus, IncidentType
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.server_admission import (
    ADMISSION_FAILURE_REASON,
    PROVISIONING_PHASE_COMPLETE,
    PROVISIONING_PHASE_LABEL,
    ServerAdmissionRejection,
    provisioning_failed_server_handles,
    server_admission_rejection,
    server_admits_application,
)
from shared.tests.server_admission_cases import (
    ADMISSION_CASES,
    CAPACITY_REASONS,
    admission_case_incidents,
    admission_case_server,
)

_NOW = datetime.now(UTC)


def _incident(**overrides) -> IncidentDTO:
    base = {
        "id": 1,
        "server_handle": "srv-1",
        "incident_type": IncidentType.PROVISIONING_FAILED,
        "status": IncidentStatus.DETECTED,
        "detected_at": _NOW,
        "created_at": _NOW,
    }
    base.update(overrides)
    return IncidentDTO(**base)


@pytest.mark.parametrize("case", ADMISSION_CASES, ids=lambda case: case.name)
def test_admission_verdict_matches_the_shared_state_matrix(case):
    server = admission_case_server(case, last_health_check=_NOW)
    failed = provisioning_failed_server_handles(admission_case_incidents(case, detected_at=_NOW))

    assert server_admits_application(server, failed) is case.admitted


def test_unfinished_provisioning_is_reported_as_a_provisioning_rejection():
    """An unready host must be classifiable as infrastructure, not as capacity."""
    case = next(c for c in ADMISSION_CASES if c.name == "active_while_installing_software")
    server = admission_case_server(case, last_health_check=_NOW)

    rejection = server_admission_rejection(server, frozenset())

    assert rejection is ServerAdmissionRejection.PROVISIONING_INCOMPLETE
    assert ADMISSION_FAILURE_REASON not in CAPACITY_REASONS


def test_open_provisioning_failure_is_reported_as_a_provisioning_rejection():
    case = next(c for c in ADMISSION_CASES if c.name == "complete_but_provisioning_failed")
    server = admission_case_server(case, last_health_check=_NOW)
    failed = provisioning_failed_server_handles(admission_case_incidents(case, detected_at=_NOW))

    rejection = server_admission_rejection(server, failed)

    assert rejection is ServerAdmissionRejection.PROVISIONING_FAILED
    assert ADMISSION_FAILURE_REASON not in CAPACITY_REASONS


def test_every_rejection_reports_one_reason_and_it_is_not_a_capacity_reason():
    """Not managed and not-admitting status are platform state, never a shortage.

    The one reason is the whole point: a rejection-specific reason is where the
    two placement paths drifted apart, and a capacity reason for any of them
    would tell an owner the platform ran out of memory when it did not.
    """
    assert ADMISSION_FAILURE_REASON not in CAPACITY_REASONS
    assert ADMISSION_FAILURE_REASON is AllocationFailureReason.SERVER_NOT_PROVISIONED
    assert set(ServerAdmissionRejection) == {
        ServerAdmissionRejection.NOT_MANAGED,
        ServerAdmissionRejection.STATUS_NOT_ADMITTING,
        ServerAdmissionRejection.PROVISIONING_INCOMPLETE,
        ServerAdmissionRejection.PROVISIONING_FAILED,
    }


def test_an_admission_refusal_is_a_bounded_wait_not_an_owner_verdict():
    """The disposition this card must not change: infrastructure wait."""
    assert (
        ALLOCATION_DISPOSITIONS[ADMISSION_FAILURE_REASON] is AttemptDisposition.INFRASTRUCTURE_WAIT
    )


def test_only_provisioning_failures_of_that_server_block_it():
    """Another server's failure, and other incident types, are not this host's."""
    handles = provisioning_failed_server_handles(
        [
            _incident(server_handle="other-srv"),
            _incident(id=2, server_handle="srv-1", incident_type=IncidentType.SERVER_UNREACHABLE),
            _incident(
                id=3, server_handle=None, incident_type=IncidentType.PROVIDER_API_UNAVAILABLE
            ),
        ]
    )

    assert handles == frozenset({"other-srv"})


def test_provisioning_phase_label_is_read_from_server_labels():
    """The rule reads the label the provisioner writes, without a schema change."""
    case = next(c for c in ADMISSION_CASES if c.name == "provisioned_active_server")
    server = admission_case_server(case, last_health_check=_NOW)

    assert server.labels[PROVISIONING_PHASE_LABEL] == PROVISIONING_PHASE_COMPLETE
    assert server_admission_rejection(server, frozenset()) is None
