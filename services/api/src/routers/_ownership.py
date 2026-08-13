"""Reading a project's initiating run inside the API.

The rule itself lives in `shared.contracts.dto.project.require_initiating_run`;
this only gives its refusal an HTTP shape, so an admin who asks a project that
predates run ownership for a worker is told why instead of seeing a 500.
"""

from fastapi import HTTPException, status

from shared.contracts.dto.project import ProjectPredatesRunOwnership, require_initiating_run
from shared.models import Project


def initiating_run_or_conflict(project: Project) -> str:
    """The run that will own the workers this request creates.

    409 rather than 422: the request is well-formed, the project is simply not
    in a state where a worker can be attributed to anything.
    """
    try:
        return require_initiating_run(project)
    except ProjectPredatesRunOwnership as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
