"""GitHub webhook receiver: an authenticated workflow completion wakes the PR/CI sweep."""

import hashlib
import hmac
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
import structlog

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.redis.client import RedisStreamClient

from ..config import get_settings
from ..dependencies import get_redis_client
from ..scheduler_wake import publish_wake
from ..schemas.github_webhook import GitHubWorkflowRunCompleted, GitHubWorkflowRunDelivery

logger = structlog.get_logger()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_DELIVERY_BYTES = 1024 * 1024
_SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})")
_MALFORMED = "Malformed webhook delivery"


def _refuse(status_code: int, reason: str, detail: str) -> HTTPException:
    """Log a refusal by its server-owned reason only; nothing the sender wrote is echoed."""
    logger.warning("github_webhook_refused", reason=reason, status_code=status_code)
    return HTTPException(status_code=status_code, detail=detail)


async def _bounded_body(request: Request) -> bytes:
    """The exact delivery bytes, refused once the stream passes the bound, whatever its length."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_DELIVERY_BYTES:
            raise _refuse(
                status.HTTP_413_CONTENT_TOO_LARGE, "body_too_large", "Webhook delivery too large"
            )
        body += chunk
    return bytes(body)


def _verify_signature(request: Request, body: bytes) -> None:
    values = request.headers.getlist("x-hub-signature-256")
    if not values:
        reason = "signature_missing"
    elif len(values) > 1:
        reason = "signature_ambiguous"
    elif (match := _SIGNATURE.fullmatch(values[0])) is None:
        reason = "signature_malformed"
    else:
        secret = get_settings().github_webhook_secret.get_secret_value().encode()
        expected = hmac.new(secret, body, hashlib.sha256).digest()
        if hmac.compare_digest(expected, bytes.fromhex(match.group(1))):
            return
        reason = "signature_mismatch"
    raise _refuse(status.HTTP_403_FORBIDDEN, reason, "Invalid webhook signature")


def _is_json_media_type(request: Request) -> bool:
    values = request.headers.getlist("content-type")
    if len(values) != 1:
        return False
    media_type, *parameters = (part.strip() for part in values[0].split(";"))
    if media_type.lower() != "application/json":
        return False
    if not parameters:
        return True
    if len(parameters) > 1:
        return False
    name, _, value = parameters[0].partition("=")
    return name.strip().lower() == "charset" and value.strip().strip('"').lower() == "utf-8"


def _refuse_non_json_constant(name: str) -> None:
    # Python's decoder accepts NaN and the infinities, which RFC 8259 JSON does not.
    raise ValueError("non-JSON numeric constant")


def _json_object(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_refuse_non_json_constant)
    except (ValueError, RecursionError):
        raise _refuse(status.HTTP_400_BAD_REQUEST, "payload_not_json", _MALFORMED) from None
    if not isinstance(payload, dict):
        raise _refuse(status.HTTP_400_BAD_REQUEST, "payload_not_object", _MALFORMED)
    return payload


def _is_completed_workflow_run(payload: dict) -> bool:
    try:
        if GitHubWorkflowRunDelivery.model_validate(payload).action != "completed":
            return False
        GitHubWorkflowRunCompleted.model_validate(payload)
    except ValidationError:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "payload_invalid", _MALFORMED) from None
    return True


@router.post("/github", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def receive_github_webhook(
    request: Request,
    redis: RedisStreamClient = Depends(get_redis_client),
) -> Response:
    """Wake the PR/CI sweep for a signed `workflow_run` completion of any conclusion.

    The signature over the raw body is the only credential this route accepts. The hint is the
    only effect: the payload selects, authorizes and fetches nothing.
    """
    body = await _bounded_body(request)
    _verify_signature(request, body)
    if not _is_json_media_type(request):
        raise _refuse(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "Webhook delivery must be JSON",
        )
    payload = _json_object(body)
    events = request.headers.getlist("x-github-event")
    if len(events) != 1:
        raise _refuse(status.HTTP_400_BAD_REQUEST, "event_header_invalid", _MALFORMED)
    if events[0] != "workflow_run":
        kind = "ping" if events[0] == "ping" else "unsupported_event"
        logger.info("github_webhook_ignored", event_kind=kind)
    elif _is_completed_workflow_run(payload):
        await publish_wake(redis, SchedulerWakeTarget.PR_CI, source="github_workflow_run")
    else:
        logger.info("github_webhook_ignored", event_kind="workflow_run_action")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
