"""Deploy failure classification, routing, and retry tracking."""

from __future__ import annotations

from datetime import UTC, datetime
import os

from langchain_openai import ChatOpenAI
import structlog

from shared.config_store import ConfigStore
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.engineering import EngineeringMessage
from shared.notifications import notify_admins
from shared.queues import ENGINEERING_QUEUE
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.story_worker_registry import clear_story_worker, get_story_worker
from ..clients.worker_spawner import delete_worker
from ._events import publish_callback_event

logger = structlog.get_logger(__name__)

_config: ConfigStore | None = None


def _get_config() -> ConfigStore:
    global _config  # noqa: PLW0603
    if _config is None:
        import os

        api_base_url = os.getenv("API_BASE_URL")
        if not api_base_url:
            raise RuntimeError("API_BASE_URL is not set")
        _config = ConfigStore(api_base_url)
    return _config


def _max_deploy_fix_attempts() -> int:
    return _get_config().get_int("deploy.max_deploy_fix_attempts", default=2)


def _max_deploy_retries() -> int:
    return _get_config().get_int("deploy.max_deploy_retries", default=3)


def _deploy_retry_ttl() -> int:
    return _get_config().get_int("deploy.deploy_retry_ttl", default=86400)


CLASSIFY_PROMPT = """\
Classify this deployment failure into one of three categories.

CODE_FIX = application bug that a developer can fix by changing code \
(import error, crash, missing dependency, wrong config value, syntax error, \
broken migration SQL, unhandled exception at startup, test failure)
RETRY = transient infrastructure issue that may self-resolve on retry \
(SSH timeout, healthcheck slow start, network unreachable temporarily, \
Docker pull timeout, DNS resolution timeout, brief resource contention)
GIVE_UP = persistent infrastructure or configuration issue that will NOT self-heal and \
cannot be fixed by changing code (port already in use/allocated, disk full, \
server out of memory, misconfigured secrets, SSL certificate error, \
permanent DNS failure, firewall blocking, container runtime broken)

Error details:
{error_details}

Reply with exactly one word: CODE_FIX, RETRY, or GIVE_UP"""


async def _classify_deploy_failure(error_details: str) -> str:
    """Use LLM to classify a deploy failure.

    Returns "CODE_FIX", "RETRY", or "GIVE_UP". Defaults to "RETRY" on any error
    (safer than CODE_FIX — retrying wastes less time than dispatching a useless worker).
    """
    try:
        api_key = os.environ.get("OPEN_ROUTER_KEY")
        if not api_key:
            logger.warning("deploy_classify_no_api_key")
            return "RETRY"

        llm = ChatOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            model="anthropic/claude-haiku-4-5",
            temperature=0.0,
            max_tokens=10,
        )
        response = await llm.ainvoke(CLASSIFY_PROMPT.format(error_details=error_details[:2000]))
        classification = response.content.strip().upper()

        valid_classifications = ("CODE_FIX", "RETRY", "GIVE_UP")
        if classification not in valid_classifications:
            logger.warning("deploy_classify_unexpected", raw=classification)
            return "RETRY"

        logger.info(
            "deploy_failure_classified",
            classification=classification,
            error_preview=error_details[:200],
        )
        return classification
    except Exception:
        logger.warning("deploy_classify_error", exc_info=True)
        return "RETRY"


async def _transition_story_safe(story_id: str, action: str) -> None:
    """Transition story status, logging errors without raising."""
    if not story_id:
        return
    try:
        await api_client.transition_story(story_id, action)
        logger.info("story_transitioned", story_id=story_id, action=action)
    except Exception:
        logger.warning("story_transition_failed", story_id=story_id, action=action, exc_info=True)


async def _redispatch_to_engineering(
    *,
    redis: RedisStreamClient,
    msg: DeployMessage,
    error_details: str,
) -> bool:
    """Re-dispatch a fix task to engineering when deploy fails due to a code bug.

    Returns True if re-dispatched, False if retry limit reached.
    """
    attempt = msg.deploy_fix_attempt
    if attempt >= _max_deploy_fix_attempts():
        logger.warning(
            "deploy_fix_retries_exhausted",
            task_id=msg.task_id,
            project_id=msg.project_id,
            attempt=attempt,
        )
        return False

    fix_task_id = f"eng-deploy-fix-{msg.task_id}-{attempt + 1}"

    # Create a run record for the fix task
    try:
        await api_client.post(
            "runs/",
            json={
                "id": fix_task_id,
                "type": RunType.ENGINEERING.value,
                "project_id": msg.project_id,
                "status": RunStatus.QUEUED.value,
            },
        )
    except Exception:
        logger.warning("deploy_fix_run_create_failed", fix_task_id=fix_task_id, exc_info=True)

    fix_msg = EngineeringMessage(
        task_id=fix_task_id,
        project_id=msg.project_id,
        user_id=msg.user_id,
        action="fix",
        description=(
            f"Deploy failed — fix the code so containers start cleanly.\n\n"
            f"Error: {error_details}\n\n"
            f"Run the service locally or check imports/dependencies before pushing."
        ),
        skip_deploy=False,
        story_id=msg.story_id or None,
        deploy_fix_attempt=attempt + 1,
    )

    await redis.publish_message(ENGINEERING_QUEUE, fix_msg)
    logger.info(
        "deploy_fix_redispatched",
        fix_task_id=fix_task_id,
        project_id=msg.project_id,
        attempt=attempt + 1,
    )
    return True


async def _handle_give_up(
    *,
    story_id: str,
    task_id: str,
    project_id: str,
    error_details: str,
    redis: RedisStreamClient,
) -> None:
    """Handle GIVE_UP classification — terminal failure, admin notified.

    The deploy failure is a persistent config/infra issue that won't self-heal
    and can't be fixed by changing code. Stop the pipeline and escalate.
    """
    logger.warning(
        "deploy_give_up",
        task_id=task_id,
        project_id=project_id,
        story_id=story_id,
        error_preview=error_details[:200],
    )

    # Story → failed (terminal)
    await _transition_story_safe(story_id, "fail")

    # Clean up worker if one exists
    if story_id:
        try:
            worker_id = await get_story_worker(redis.redis, story_id)
            if worker_id:
                await delete_worker(worker_id, reason="failed")
                await clear_story_worker(redis.redis, story_id)
        except Exception:
            logger.warning("give_up_worker_cleanup_failed", story_id=story_id, exc_info=True)

    # Notify admin (HITL required)
    try:
        await notify_admins(
            f"Deploy GIVE_UP for task {task_id} (project {project_id}):\n{error_details[:500]}",
            level="error",
        )
    except Exception:
        logger.warning("give_up_admin_notify_failed", task_id=task_id, exc_info=True)


async def _route_deploy_failure(
    *,
    classification: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
    error_details: str,
    story_id: str,
) -> None:
    """Route a deploy failure based on three-way classification.

    CODE_FIX → redispatch to engineering worker
    RETRY → do nothing (caller handles retry counter via _handle_deploy_failure)
    GIVE_UP → terminal failure, escalate to admin
    """
    if classification == "CODE_FIX":
        await _transition_story_safe(story_id, "start")
        await _redispatch_to_engineering(
            redis=redis,
            msg=msg,
            error_details=error_details,
        )
    elif classification == "GIVE_UP":
        await _handle_give_up(
            story_id=story_id,
            task_id=msg.task_id,
            project_id=msg.project_id,
            error_details=error_details,
            redis=redis,
        )
    # RETRY: caller handles via _handle_deploy_failure / _track_deploy_retry


async def _track_deploy_retry(*, redis: RedisStreamClient, story_id: str) -> None:
    """Increment deploy retry counter and transition story.

    After _max_deploy_retries() failures, marks story as failed (HITL).
    Otherwise rolls story back to "start" for another deploy attempt.
    """
    if not story_id:
        await _transition_story_safe(story_id, "start")
        return

    attempt_key = f"deploy:{story_id}:attempts"
    attempts = await redis.redis.incr(attempt_key)
    await redis.redis.expire(attempt_key, _deploy_retry_ttl())

    if attempts >= _max_deploy_retries():
        logger.warning(
            "deploy_max_retries_exceeded",
            story_id=story_id,
            attempts=attempts,
            max_retries=_max_deploy_retries(),
        )
        await _transition_story_safe(story_id, "fail")
        try:
            worker_id = await get_story_worker(redis.redis, story_id)
            if worker_id:
                await delete_worker(worker_id, reason="failed")
                logger.info(
                    "story_worker_deleted_on_fail",
                    story_id=story_id,
                    worker_id=worker_id,
                )
            await clear_story_worker(redis.redis, story_id)
        except Exception as e:
            logger.warning("story_worker_cleanup_failed", story_id=story_id, error=str(e))
    else:
        logger.info(
            "deploy_failure_rollback",
            story_id=story_id,
            attempt=attempts,
            max_retries=_max_deploy_retries(),
        )
        await _transition_story_safe(story_id, "start")


async def _handle_deploy_failure(
    *,
    task_id: str,
    project_id: str,
    error_msg: str,
    story_id: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
) -> dict:
    """Common handler for deploy failures — update run, rollback story, notify.

    Tracks consecutive deploy failures per story in Redis. After _max_deploy_retries()
    failures, transitions story to failed instead of back to in_progress (prevents
    infinite deploy-fail-retry loops).
    """
    await api_client.patch(
        f"runs/{task_id}",
        json={"status": "failed", "error_message": error_msg},
    )
    await _track_deploy_retry(redis=redis, story_id=story_id)

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id or "",
    )
    # No proactive message — deploy failures are internal (retried automatically)

    return {
        "status": "failed",
        "error": error_msg,
        "finished_at": datetime.now(UTC).isoformat(),
    }
