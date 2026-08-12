"""The control-plane allowlist is a closed decision, not a list of known holes."""

from typing import get_args

import pytest

from shared.contracts.queues.worker import WorkerConfig
from shared.contracts.vocab import WorkerType
from shared.contracts.worker_control_plane import (
    DOCKER_DAEMON_OPERATIONS,
    TURN_PROTOCOL_OPERATIONS,
    WORKER_TYPE_CONTROL_PLANE_ALLOWLIST,
    WorkerControlPlaneOperation,
    control_plane_denial,
)


def test_the_worker_type_enum_and_the_wire_literal_cannot_drift():
    assert set(get_args(WorkerConfig.model_fields["worker_type"].annotation)) == {
        worker_type.value for worker_type in WorkerType
    }


def test_every_worker_type_has_a_decision():
    assert set(WORKER_TYPE_CONTROL_PLANE_ALLOWLIST) == set(WorkerType)


def test_every_operation_is_classified():
    """A new control-plane operation must be placed, not left implicitly allowed.

    Adding one to the enum without saying whether it touches the management
    host's Docker daemon fails here, which is the point: the QA allowlist is
    derived from that classification.
    """
    assert TURN_PROTOCOL_OPERATIONS | DOCKER_DAEMON_OPERATIONS == set(WorkerControlPlaneOperation)
    assert not TURN_PROTOCOL_OPERATIONS & DOCKER_DAEMON_OPERATIONS


def test_a_qa_worker_gets_the_turn_protocol_and_nothing_that_reaches_the_daemon():
    allowed = WORKER_TYPE_CONTROL_PLANE_ALLOWLIST[WorkerType.QA]
    assert allowed == TURN_PROTOCOL_OPERATIONS
    assert not allowed & DOCKER_DAEMON_OPERATIONS
    for operation in DOCKER_DAEMON_OPERATIONS:
        assert (
            control_plane_denial(WorkerType.QA.value, operation)
            == f"a qa worker may not call {operation.value}"
        )


def test_a_developer_worker_keeps_every_operation():
    for operation in WorkerControlPlaneOperation:
        assert control_plane_denial(WorkerType.DEVELOPER.value, operation) is None


@pytest.mark.parametrize("recorded", [None, "", "developer ", "DEVELOPER", "admin", "qa;developer"])
def test_an_unrecorded_or_unknown_type_is_denied_everything(recorded):
    for operation in WorkerControlPlaneOperation:
        assert (
            control_plane_denial(recorded, operation)
            == "worker type is not recorded for this worker"
        )
