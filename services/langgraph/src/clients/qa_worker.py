"""Running one exploratory QA run on the assigned subscription coding agent.

There is no second way of starting agents here. This asks worker-manager for a
container exactly as `worker_spawner` does — the same `worker:commands` stream,
the same create/status/delete commands, the same broker — and differs only in
what it asks for: a `qa` worker, which has no repository, no git credentials and
nothing to commit, and whose whole reach into the deployment is the capability
endpoint URL and token it is handed in its environment.

The credentials of the agent itself never come near this: the subscription
session is a host directory worker-manager mounts into the container on the
management host, and neither this process nor the target ever holds it.

The verdict does not travel on the worker output stream. That stream carries the
developer-worker result contract, which describes a commit, and a QA run makes
none — so the run's answer comes back through the capability endpoint, and the
output stream is read only for what it can actually tell us: whether an executor
ever got going.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
import uuid

import redis.asyncio as redis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerConfig,
)
from shared.log_config import get_logger
from shared.queues import WORKER_COMMANDS, WORKER_RESPONSES
from shared.redis.client import DEFAULT_STREAM_MAXLEN

from ..config.settings import get_settings
from .worker_spawner import CREATION_TIMEOUT, _wait_for_response, _wait_until_ready

logger = get_logger(__name__)

# How long the run's answer is still accepted after the container has finished.
# The agent submits its verdict and then exits; the two arrive over different
# channels, so the faster one must not decide the run.
VERDICT_GRACE_S = 15


class QAExecutorUnavailable(Exception):
    """No executor ran: the container never started, or died before working.

    `transient` says whether trying again could plausibly answer differently. A
    container that lost a race with a busy host is transient; a subscription
    session that is absent or expired is not, and retrying it only spends time
    before the same infrastructure outcome.
    """

    def __init__(self, detail: str, *, transient: bool) -> None:
        super().__init__(detail)
        self.detail = detail
        self.transient = transient


@dataclass(frozen=True)
class QAExecutorRun:
    """What the executor did, as the runtime saw it."""

    verdict_submitted: bool
    calls_served: int
    detail: str
    transcript: str = ""


# Substrings in a worker-manager failure that mean "this host's agent session is
# not usable", rather than "this attempt was unlucky". They come from
# `codex_auth.validate_codex_host_session` and the wrapper's own
# `validate_agent_config`, which are the two places a session is checked.
_SESSION_FAILURE_MARKERS = (
    "CLAUDE_CONFIG_DIR",
    "HOST_CLAUDE_DIR",
    "codex",
    "session",
    "auth",
)


def _classify_start_failure(detail: str) -> QAExecutorUnavailable:
    lowered = detail.lower()
    permanent = any(marker.lower() in lowered for marker in _SESSION_FAILURE_MARKERS)
    return QAExecutorUnavailable(detail, transient=not permanent)


async def run_qa_executor(
    *,
    agent_type: AgentType,
    capability_url: str,
    capability_token: str,
    instructions: str,
    prompt: str,
    verdict_received: asyncio.Event,
    calls_served: Callable[[], int],
    timeout: int,
) -> QAExecutorRun:
    """Run one exploratory QA pass on a central ephemeral coding agent.

    Args:
        agent_type: the assigned executor. Claude Code unless something assigned
            Codex explicitly.
        capability_url: this run's capability endpoint, the container's only
            route to the deployment.
        capability_token: the run-scoped credential for that endpoint. It grants
            nothing after the run: the endpoint stops with it.
        instructions: the QA rules, written to the agent's instruction file.
        prompt: the run's task, carrying the acceptance criteria.
        verdict_received: set by the capability endpoint when the agent submits
            its result.
        calls_served: the endpoint's live call counter, read after the run to
            tell "no executor ran" from "an executor ran and said nothing".
        timeout: seconds the executor is given to reach a verdict.

    Raises:
        QAExecutorUnavailable: no executor ran at all.
    """
    settings = get_settings()
    request_id = str(uuid.uuid4())
    worker_id = f"qa-{request_id[:12]}"
    group_name = f"qa-client-{request_id[:8]}"
    consumer_id = f"qa-{request_id[:8]}"
    redis_client = redis.from_url(settings.redis_url)
    created = False

    try:
        try:
            await redis_client.xgroup_create(WORKER_RESPONSES, group_name, id="$", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        create_cmd = CreateWorkerCommand(
            request_id=request_id,
            config=WorkerConfig(
                name=worker_id,
                worker_type="qa",
                agent_type=agent_type,
                instructions=instructions,
                task_content=prompt,
                allowed_commands=["*"],
                # No git, no GitHub CLI, no HTTP client capability: a QA
                # executor has no repository to touch and one way to reach the
                # deployment, which needs nothing the base image lacks.
                capabilities=[],
                # The whole environment a QA executor is given. There is no
                # GitHub token, no API key and no repository here: a QA run
                # writes nothing anywhere, and the only address it holds is an
                # endpoint that outlives neither the run nor this process.
                env_vars={
                    "QA_CAPABILITY_URL": capability_url,
                    "QA_CAPABILITY_TOKEN": capability_token,
                },
            ),
            context={"source": "qa-worker"},
        )
        await redis_client.xadd(WORKER_COMMANDS, {"data": create_cmd.model_dump_json()})
        created = True
        logger.info("qa_executor_requested", worker_id=worker_id, agent_type=agent_type.value)

        ack = await _wait_for_response(
            redis_client, group_name, consumer_id, request_id, CREATION_TIMEOUT
        )
        if not ack:
            raise QAExecutorUnavailable(
                f"worker-manager did not acknowledge the QA executor within {CREATION_TIMEOUT}s",
                transient=True,
            )
        if not ack.get("success"):
            raise _classify_start_failure(str(ack.get("error") or "worker-manager refused"))

        ready_failure = await _wait_until_ready(redis_client, worker_id, request_id)
        if ready_failure:
            raise _classify_start_failure(ready_failure.output)

        output_stream = f"worker:{worker_id}:output"
        try:
            await redis_client.xgroup_create(output_stream, group_name, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

        await redis_client.xadd(
            f"worker:{worker_id}:input",
            {"data": json.dumps({"request_id": request_id, "prompt": prompt, "user_id": 0})},
            maxlen=DEFAULT_STREAM_MAXLEN,
            approximate=True,
        )
        logger.info("qa_executor_started", worker_id=worker_id, timeout=timeout)

        transcript = await _await_verdict_or_exit(
            redis_client=redis_client,
            group_name=group_name,
            consumer_id=consumer_id,
            output_stream=output_stream,
            worker_id=worker_id,
            verdict_received=verdict_received,
            timeout=timeout,
        )
        served = calls_served()
        if not verdict_received.is_set() and served == 0:
            raise QAExecutorUnavailable(
                f"the QA executor container ran but never reached the capability endpoint: "
                f"{transcript[:1000] or 'no output'}",
                transient=True,
            )
        return QAExecutorRun(
            verdict_submitted=verdict_received.is_set(),
            calls_served=served,
            detail=f"{agent_type.value} executor {worker_id}",
            transcript=transcript,
        )
    finally:
        if created:
            await redis_client.xadd(
                WORKER_COMMANDS,
                {
                    "data": DeleteWorkerCommand(
                        request_id=f"cleanup-{request_id}",
                        worker_id=worker_id,
                        reason="completed",
                    ).model_dump_json()
                },
            )
            logger.info("qa_executor_deleted", worker_id=worker_id)
        for stream in (WORKER_RESPONSES, f"worker:{worker_id}:output"):
            try:
                await redis_client.xgroup_destroy(stream, group_name)
            except Exception as exc:  # noqa: BLE001 — cleanup of a group that may not exist
                logger.debug("qa_executor_group_cleanup_failed", stream=stream, error=str(exc))
        await redis_client.aclose()


async def _await_verdict_or_exit(
    *,
    redis_client: redis.Redis,
    group_name: str,
    consumer_id: str,
    output_stream: str,
    worker_id: str,
    verdict_received: asyncio.Event,
    timeout: int,
) -> str:
    """Wait for the run's answer, or for the container to stop having one.

    Two things end a run and they arrive over different channels: the verdict on
    the capability endpoint, and the container's own exit on the output stream.
    Whichever lands first, the other is given a short grace — an agent that
    submitted its verdict and then exited must not be read as an agent that
    exited without one.

    Returns whatever the container reported about itself, for the diagnostic
    record and for the write-guard scan.
    """
    verdict_task = asyncio.create_task(verdict_received.wait())
    output_task = asyncio.create_task(
        _wait_for_response(
            redis_client,
            group_name,
            consumer_id,
            None,
            float(timeout),
            output_stream,
            worker_id=worker_id,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {verdict_task, output_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if verdict_task in done and not output_task.done():
            # The agent answered; give the container a moment to finish, but do
            # not hold the run open for it.
            await asyncio.wait({output_task}, timeout=VERDICT_GRACE_S)
        elif output_task in done and not verdict_task.done():
            await asyncio.wait({verdict_task}, timeout=VERDICT_GRACE_S)
        return _transcript_of(output_task)
    finally:
        for task in (verdict_task, output_task):
            task.cancel()


def _transcript_of(output_task: asyncio.Task) -> str:
    """The container's own account of the run, if it produced one."""
    if not output_task.done() or output_task.cancelled():
        return ""
    try:
        payload = output_task.result()
    except Exception as exc:  # noqa: BLE001 — a poison payload is still evidence
        return f"worker output could not be read: {exc}"
    if not payload:
        return ""
    return json.dumps(payload)[:20000]
