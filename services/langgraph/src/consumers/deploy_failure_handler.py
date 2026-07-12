"""Deploy failure classification — pure run.status/result updates.

Story lifecycle routing (CODE_FIX → engineering, RETRY → redeploy, GIVE_UP → fail)
is managed by the dispatcher's supervise_deploying_stories().
"""

from __future__ import annotations

from datetime import UTC, datetime
import os

from langchain_openai import ChatOpenAI
import structlog

from shared.config_store import ConfigStore
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._events import publish_callback_event

logger = structlog.get_logger(__name__)

_config: ConfigStore | None = None


def _get_config() -> ConfigStore:
    global _config  # noqa: PLW0603
    if _config is None:
        api_base_url = os.getenv("API_BASE_URL")
        if not api_base_url:
            raise RuntimeError("API_BASE_URL is not set")
        _config = ConfigStore(api_base_url)
    return _config


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


def _classification_to_outcome(classification: str) -> DeployOutcome:
    """Map LLM classification string to DeployOutcome enum."""
    return {
        "CODE_FIX": DeployOutcome.CODE_FIX,
        "RETRY": DeployOutcome.RETRY,
        "GIVE_UP": DeployOutcome.GIVE_UP,
    }.get(classification, DeployOutcome.RETRY)


async def _handle_deploy_failure(
    *,
    task_id: str,
    project_id: str,
    error_msg: str,
    story_id: str,
    callback_stream: str,
    user_id: str,
    redis: RedisStreamClient,
    deploy_outcome: DeployOutcome = DeployOutcome.RETRY,
    deploy_fix_attempt: int = 0,
) -> dict:
    """Update run status/result on deploy failure.

    Stores deploy_outcome and error_details in run.result for
    the dispatcher to read and route story lifecycle.
    """
    run_result = DeployRunResult(
        deploy_outcome=deploy_outcome,
        error_details=error_msg,
        deploy_fix_attempt=deploy_fix_attempt,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": run_result.model_dump(mode="json"),
        },
    )

    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        user_id=user_id,
        project_id=project_id or "",
    )

    return {
        "status": "failed",
        "error": error_msg,
        "finished_at": datetime.now(UTC).isoformat(),
    }
