"""What a worker's broker credential is allowed to ask the control plane for.

A worker's broker token is not a secret from the agent running inside its
container: the CLI is a child of the wrapper under the same user, so
`/proc/<ppid>/environ` hands it both the broker URL and the token. Removing the
variable from the agent's subprocess environment is therefore not a boundary —
it is a tidy-up. The boundary is here: what that token is permitted to name.

A QA executor has no control-plane authority beyond the protocol of its own
turn. Anything that can create a container, start a build, or otherwise reach
the management host's Docker daemon is refused to it. The refusal is stated as
an allowlist per worker type rather than a denylist of known-dangerous routes,
so a control-plane operation added later is refused to QA until someone decides
otherwise, instead of being open until someone remembers.

The worker type used here is always the one the server recorded when it created
the worker — never a field the caller supplies about itself.
"""

from enum import StrEnum

from shared.contracts.vocab import WorkerType


class WorkerControlPlaneOperation(StrEnum):
    """Every operation a worker credential can name, one per route.

    Values are route-independent names on purpose: two boundaries (the broker
    and worker-manager) authorize the same operation, and they must not have to
    agree on a URL shape to agree on a decision.
    """

    INPUT_LEASE = "input.lease"
    OUTPUT_SUBMIT = "output.submit"
    STATUS_UPDATE = "status.update"
    SESSION_READ = "session.read"
    SESSION_WRITE = "session.write"
    SESSION_CLEAR = "session.clear"
    INFRA_COMPOSE = "infra.compose"


# The protocol of a turn: take work, report progress, keep the CLI's session
# handle, hand back a typed result. None of it can create anything on the
# management host.
TURN_PROTOCOL_OPERATIONS: frozenset[WorkerControlPlaneOperation] = frozenset(
    {
        WorkerControlPlaneOperation.INPUT_LEASE,
        WorkerControlPlaneOperation.OUTPUT_SUBMIT,
        WorkerControlPlaneOperation.STATUS_UPDATE,
        WorkerControlPlaneOperation.SESSION_READ,
        WorkerControlPlaneOperation.SESSION_WRITE,
        WorkerControlPlaneOperation.SESSION_CLEAR,
    }
)

# Operations that reach the management host's Docker daemon. A developer worker
# needs them: running its project's compose stack is how it verifies its own
# change. A QA executor tests a deployed application from outside and has no
# project of its own, so for it these are pure escalation.
DOCKER_DAEMON_OPERATIONS: frozenset[WorkerControlPlaneOperation] = frozenset(
    {WorkerControlPlaneOperation.INFRA_COMPOSE}
)

WORKER_TYPE_CONTROL_PLANE_ALLOWLIST: dict[WorkerType, frozenset[WorkerControlPlaneOperation]] = {
    WorkerType.DEVELOPER: frozenset(WorkerControlPlaneOperation),
    WorkerType.QA: TURN_PROTOCOL_OPERATIONS,
}


def control_plane_denial(
    recorded_worker_type: str | None,
    operation: WorkerControlPlaneOperation,
) -> str | None:
    """Why this operation is refused for this recorded worker type, or `None`.

    Fails closed on purpose. A worker whose type the server did not record is
    not an old worker to be waved through — the record is written before the
    credential that reaches this function exists, so its absence means the
    caller is not a worker this installation created.
    """
    try:
        worker_type = WorkerType(recorded_worker_type)
    except ValueError:
        return "worker type is not recorded for this worker"

    if operation in WORKER_TYPE_CONTROL_PLANE_ALLOWLIST[worker_type]:
        return None
    return f"a {worker_type.value} worker may not call {operation.value}"
