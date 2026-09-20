"""The Compose project one worker owns, and what that ownership includes.

A worker's bounded compose plan runs under one project name derived from the
worker id, and everything Docker creates for that plan carries it as
`com.docker.compose.project`. That label is the only thing that attributes a
container to the worker once the plan itself is gone, so the name is declared
here rather than spelled out at each place that needs it: `compose_runner`
builds the invocations with it, the worker's teardown sweeps by it, and the
orphan garbage collector and the live suite's residue proof ask by it.

**Why a sweep by this label, and not `docker compose down`.** `down` removes the
services of the plan; it does not remove the one-shot containers
`docker compose run` creates, which Compose names `<project>-<service>-run-<id>`
and marks `com.docker.compose.oneoff=True`. The generated product's
`make test-integration` is exactly that shape, and two such containers survived
a completed story on production for 7+ hours and then survived a full project
teardown as well (`issue:868e40fc0377b0dabb77`). The label is what finds them.
"""

from __future__ import annotations

#: Docker's own label for the Compose project a container belongs to.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
#: Docker's own label for a container `docker compose run` created rather than
#: `up`. Read for reporting; the sweep is by project, because a project's
#: containers all belong to the worker whatever created them.
COMPOSE_ONEOFF_LABEL = "com.docker.compose.oneoff"
#: The infix Compose puts in a one-shot container's name.
COMPOSE_ONEOFF_NAME_INFIX = "-run-"


def worker_compose_project(worker_id: str) -> str:
    """The Compose project name a worker's bounded plan runs under."""
    return f"worker_{worker_id}"


def worker_compose_project_filter(worker_id: str) -> dict[str, str]:
    """The Docker label filter selecting everything that plan created."""
    return {"label": f"{COMPOSE_PROJECT_LABEL}={worker_compose_project(worker_id)}"}


def worker_id_of_compose_project(project: str) -> str | None:
    """The worker a Compose project name belongs to, or None if it is not a worker's.

    The inverse of `worker_compose_project`, and deliberately strict: the orphan
    sweep reads this off every container that carries a Compose project label,
    and a project that is not a worker's — the orchestrator's own stack, a
    product deployment — must come back as None rather than as a worker id
    nobody knows, which would read as an orphan.
    """
    prefix = worker_compose_project("")
    if not project.startswith(prefix):
        return None
    return project[len(prefix) :] or None
