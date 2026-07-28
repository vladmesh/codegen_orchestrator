"""Read one environment slot out of a deployed service, over the existing SSH path.

This is the answer to "is the value actually gone from the running service?".
Nobody can answer it from a deploy's own result: a deploy is a request handed to
GitHub Actions, and what the containers ended up with is a separate fact. So the
question is put to the server, through the key and the playbooks this service
already has.

The reading never changes anything, which is what makes repeating it free: a
caller that cannot confirm an answer asks again.
"""

from __future__ import annotations

import asyncio
import re

import structlog

from shared.contracts.queues.env_observation import (
    EnvObservationOutcome,
    EnvObservationRequest,
    EnvObservationResult,
)

from .ansible_runner import AnsibleRunner
from .api_client import get_server_info, get_server_ssh_key

logger = structlog.get_logger(__name__)

OBSERVE_PLAYBOOK = "observe_service_env.yml"
OBSERVATION_TIMEOUT_SECONDS = 300

# The one line the playbook prints for this reader. Ansible says a great deal
# else, and none of it is the answer.
_MARKER = re.compile(r"ENV_OBSERVATION containers=(\d+) filled=([01])")

# Enough of a failure to act on, not enough to paste a server's output into a
# grant record.
_DETAIL_LENGTH = 300


def parse_observation(output: str) -> tuple[int, bool]:
    """Return (running containers, slot filled) from a playbook run's output.

    Raises:
        ValueError: the run said nothing this reader recognises, which is a
            reading that did not happen rather than an empty slot.
    """
    matches = _MARKER.findall(output)
    if not matches:
        raise ValueError("the playbook produced no ENV_OBSERVATION line")
    containers, filled = matches[-1]
    return int(containers), filled == "1"


def _unreachable(request: EnvObservationRequest, detail: str) -> EnvObservationResult:
    return EnvObservationResult(
        request_id=request.request_id,
        outcome=EnvObservationOutcome.UNREACHABLE,
        env_key=request.env_key,
        detail=detail[:_DETAIL_LENGTH],
    )


async def observe_service_env(request: EnvObservationRequest) -> EnvObservationResult:
    """Read *request.env_key* out of the containers the service is running with.

    Every way of not getting an answer — no server, no key, a playbook that
    failed, output without the marker, nothing running to read — comes back as
    UNREACHABLE with the reason named. None of them may be reported as an empty
    slot: the caller acts on absence, so absence has to have been seen.
    """
    log = logger.bind(
        request_id=request.request_id,
        project_id=request.project_id,
        server_handle=request.server_handle,
        env_key=request.env_key,
    )

    server = await get_server_info(request.server_handle)
    if not server.public_ip:
        return _unreachable(request, f"server {request.server_handle} has no address to reach")

    ssh_key = await get_server_ssh_key(request.server_handle)
    if not ssh_key:
        return _unreachable(request, f"no SSH key stored for server {request.server_handle}")

    runner = AnsibleRunner()
    success, output = await asyncio.to_thread(
        runner.run_playbook,
        server_ip=server.public_ip,
        server_handle=request.server_handle,
        playbook_name=OBSERVE_PLAYBOOK,
        ssh_user=server.ssh_user,
        ssh_private_key=ssh_key,
        timeout=OBSERVATION_TIMEOUT_SECONDS,
        extra_vars={"service_slug": request.service_slug, "env_key": request.env_key},
    )
    if not success:
        log.warning("env_observation_playbook_failed")
        return _unreachable(request, f"the observation playbook failed: {output}")

    try:
        containers, filled = parse_observation(output)
    except ValueError as e:
        log.warning("env_observation_unparsed")
        return _unreachable(request, str(e))

    if containers == 0:
        # A service with nothing running has no environment to read. Calling that
        # "the value is gone" would settle the question against a machine that
        # was never asked.
        return _unreachable(
            request, f"service {request.service_slug} has no running containers to read"
        )

    log.info("env_observation_complete", containers=containers, filled=filled)
    return EnvObservationResult(
        request_id=request.request_id,
        outcome=EnvObservationOutcome.OBSERVED,
        env_key=request.env_key,
        present=filled,
        containers=containers,
    )
