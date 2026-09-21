"""Infrastructure Worker — consumes from provisioner:queue.

Run standalone: python -m src.main
"""

from __future__ import annotations

import asyncio
import json
import os
import signal

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, ValidationError
import structlog

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import (
    ProvisioningFinalization,
    ProvisioningFinalizationDisposition,
)
from shared.contracts.queues.provisioner import ProvisionerMessage, ProvisionerResult
from shared.contracts.vocab import ResultStatus
from shared.crypto import SecretsCipher
from shared.log_config import setup_logging
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    managed_provider_ids,
    validate_provider_policies,
)
from shared.queues import INFRA_GROUP, PROVISIONER_QUEUE
from shared.redis import RedisStreamClient

from .provisioner.api_client import finalize_provisioning
from .provisioner.handlers import FinalizationOutcomeUnknown
from .provisioner.incidents import IncidentPersistenceError, create_incident
from .provisioner.node import ProvisionerNode

logger = structlog.get_logger(__name__)

# Consumer configuration
CONSUMER_NAME = f"infra-worker-{os.getpid()}"

# Shutdown flag
_shutdown = False
INCIDENT_OUTAGE_RETRY_BUDGET = 3
FINALIZATION_REPLAY_TTL_SECONDS = 24 * 60 * 60


class FinalizationReplayEnvelope(BaseModel):
    """One encrypted, delivery-bound finalizer command retained for PEL reclaim."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    request_id: str
    server_handle: str
    finalization: ProvisioningFinalization


def _outage_key(message_id: str) -> str:
    return f"provisioner:incident-outage:{message_id}"


def _finalization_replay_key(message_id: str) -> str:
    return f"provisioner:finalization-replay:{message_id}"


async def _retain_finalization(
    client,
    msg,
    request_id: str,
    server_handle: str,
    finalization: ProvisioningFinalization,
) -> None:
    """Encrypt and retain the exact finalizer command before its transport call."""
    envelope = FinalizationReplayEnvelope(
        message_id=msg.message_id,
        request_id=request_id,
        server_handle=server_handle,
        finalization=finalization,
    )
    ciphertext = SecretsCipher().encrypt(envelope.model_dump_json())
    await client.redis.set(
        _finalization_replay_key(msg.message_id),
        ciphertext,
        ex=FINALIZATION_REPLAY_TTL_SECONDS,
    )


def _decode_finalization_replay(raw: str | bytes) -> FinalizationReplayEnvelope:
    """Decrypt a retained command without exposing its validation input."""
    ciphertext = raw.decode() if isinstance(raw, bytes) else raw
    plaintext = SecretsCipher().decrypt(ciphertext)
    return FinalizationReplayEnvelope.model_validate_json(plaintext)


def _decode_hash(values: dict) -> dict[str, str]:
    return {
        (key.decode() if isinstance(key, bytes) else str(key)): (
            value.decode() if isinstance(value, bytes) else str(value)
        )
        for key, value in values.items()
    }


async def _publish_and_ack(client, msg, result: ProvisionerResult) -> None:
    result_key = f"deploy:result:{result.request_id}"
    await client.redis.set(result_key, result.model_dump_json(), ex=3600)
    await client.publish("provisioner:results", result.model_dump(mode="json"))
    await client.ack(PROVISIONER_QUEUE, INFRA_GROUP, msg.message_id)


async def _handle_incident_outage(
    client, msg, job: ProvisionerMessage, error: IncidentPersistenceError
) -> None:
    """Retry only the journal write and escalate after a bounded outage budget."""
    key = _outage_key(msg.message_id)
    attempts = await client.redis.hincrby(key, "attempts", 1)
    await client.redis.hset(
        key,
        mapping={
            "server_handle": error.server_handle,
            "details": json.dumps(error.details),
            "request_id": job.request_id,
        },
    )
    if attempts < INCIDENT_OUTAGE_RETRY_BUDGET:
        logger.warning(
            "provisioner_incident_journal_retry_pending",
            entry_id=msg.message_id,
            attempts=attempts,
        )
        return

    state = _decode_hash(await client.redis.hgetall(key))
    if state.get("terminal_published") == "1":
        await client.ack(PROVISIONER_QUEUE, INFRA_GROUP, msg.message_id)
        await client.redis.delete(_finalization_replay_key(msg.message_id))
        return

    result = ProvisionerResult(
        request_id=job.request_id,
        status=ResultStatus.FAILED,
        server_handle=error.server_handle,
        errors=["Provisioning incident journal unavailable after bounded retries"],
    )
    # Publish before ACK. A failed publish leaves the PEL entry retryable. A
    # persisted marker prevents duplicate delivery after an ACK failure from
    # publishing another outcome. A process crash between publish and marker is
    # still an at-least-once delivery trade-off.
    await client.redis.set(f"deploy:result:{result.request_id}", result.model_dump_json(), ex=3600)
    await client.publish("provisioner:results", result.model_dump(mode="json"))
    await client.redis.hset(key, mapping={"terminal_published": "1"})
    await client.ack(PROVISIONER_QUEUE, INFRA_GROUP, msg.message_id)
    await client.redis.delete(_finalization_replay_key(msg.message_id))


async def _retry_saved_incident(
    client, msg, job: ProvisionerMessage, state: dict[str, str]
) -> bool:
    """Return True when a reclaimed entry was handled without provisioning again."""
    key = _outage_key(msg.message_id)
    if state.get("terminal_published") == "1":
        await client.ack(PROVISIONER_QUEUE, INFRA_GROUP, msg.message_id)
        await client.redis.delete(_finalization_replay_key(msg.message_id))
        return True
    try:
        await create_incident(
            state["server_handle"], IncidentType.PROVISIONING_FAILED, json.loads(state["details"])
        )
    except IncidentPersistenceError as error:
        await _handle_incident_outage(client, msg, job, error)
        return True

    result = ProvisionerResult(
        request_id=job.request_id,
        status=ResultStatus.FAILED,
        server_handle=state["server_handle"],
        errors=["Provisioning failed; incident journal write recovered"],
    )
    await _publish_and_ack(client, msg, result)
    await client.redis.delete(key)
    await client.redis.delete(_finalization_replay_key(msg.message_id))
    return True


def handle_shutdown(signum, frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def process_provisioner_job(job_data: dict, *, retain_finalization=None) -> ProvisionerResult:
    """Process a single provisioner job.

    Args:
        job_data: Job data from Redis queue

    Returns:
        ProvisionerResult with status and details
    """
    job_id = job_data.get("job_id") or job_data.get("request_id", "unknown")
    server_handle = job_data.get("server_handle", "")

    logger.info(
        "provisioner_job_started",
        job_id=job_id,
        server_handle=server_handle,
    )

    try:
        # Build state for ProvisionerNode
        state = {
            "server_to_provision": server_handle,
            "is_incident_recovery": job_data.get("is_recovery", False),
            "provisioning_profile": job_data.get("profile"),
            "errors": [],
            "retain_finalization": retain_finalization,
        }

        # Run provisioner
        node = ProvisionerNode()
        result = await node.run(state)

        # Extract result
        provisioning_result = result.get("provisioning_result", {})
        status = provisioning_result.get("status", "unknown")

        if status == ResultStatus.SUCCESS.value:
            logger.info(
                "provisioner_job_success",
                job_id=job_id,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
            )
            return ProvisionerResult(
                request_id=job_id,
                status=ResultStatus.SUCCESS,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
                services_redeployed=provisioning_result.get("services_redeployed", 0),
            )
        elif status == ResultStatus.SUPERSEDED.value:
            # A newer attempt/episode owns this server now. This completion is a
            # no-op: publish it as first-class SUPERSEDED so downstream consumers
            # skip status mutation and failure notification instead of misreading
            # a non-success status as a failure.
            logger.info(
                "provisioner_job_superseded",
                job_id=job_id,
                server_handle=server_handle,
            )
            return ProvisionerResult(
                request_id=job_id,
                status=ResultStatus.SUPERSEDED,
                server_handle=server_handle,
                server_ip=provisioning_result.get("server_ip"),
            )
        else:
            errors = result.get("errors", ["Unknown error"])
            logger.error(
                "provisioner_job_failed",
                job_id=job_id,
                server_handle=server_handle,
                errors=errors,
            )
            return ProvisionerResult(
                request_id=job_id,
                status=ResultStatus.FAILED,
                server_handle=server_handle,
                errors=errors,
            )

    except FinalizationOutcomeUnknown:
        logger.warning(
            "provisioning_finalization_redelivery_pending",
            job_id=job_id,
            server_handle=server_handle,
        )
        raise
    except IncidentPersistenceError:
        logger.error(
            "provisioner_incident_journal_unavailable",
            job_id=job_id,
            server_handle=server_handle,
        )
        raise
    except Exception as e:
        logger.error(
            "provisioner_job_exception",
            job_id=job_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return ProvisionerResult(
            request_id=job_id,
            status=ResultStatus.FAILED,
            server_handle=server_handle,
            error=str(e),
        )


async def _fail_finalization_replay(client, msg, job, reason: str) -> None:
    """Record and publish a typed terminal failure without running provisioning."""
    await create_incident(
        job.server_handle,
        IncidentType.PROVISIONING_FAILED,
        {"step": "finalization_replay", "reason": reason},
    )
    result = ProvisionerResult(
        request_id=job.request_id,
        status=ResultStatus.FAILED,
        server_handle=job.server_handle,
        errors=[f"Provisioning finalization replay failed: {reason}"],
    )
    await _publish_and_ack(client, msg, result)
    await client.redis.delete(_finalization_replay_key(msg.message_id))


async def _retry_saved_finalization(client, msg, job, raw: str | bytes) -> None:
    """Replay one exact saved command, never the provisioning node."""
    try:
        envelope = _decode_finalization_replay(raw)
    except (InvalidToken, ValidationError, ValueError, TypeError):
        await _fail_finalization_replay(client, msg, job, "corrupt")
        return
    if (envelope.message_id, envelope.request_id, envelope.server_handle) != (
        msg.message_id,
        job.request_id,
        job.server_handle,
    ):
        await _fail_finalization_replay(client, msg, job, "command_mismatch")
        return

    try:
        disposition = await finalize_provisioning(envelope.server_handle, envelope.finalization)
    except Exception:
        logger.warning(
            "provisioning_finalization_redelivery_pending",
            entry_id=msg.message_id,
            server_handle=job.server_handle,
        )
        return

    if disposition in (
        ProvisioningFinalizationDisposition.FINALIZED,
        ProvisioningFinalizationDisposition.IDEMPOTENT,
    ):
        result = ProvisionerResult(
            request_id=job.request_id,
            status=ResultStatus.SUCCESS,
            server_handle=job.server_handle,
            server_ip=envelope.finalization.proved_identity.public_ip,
        )
    else:
        await create_incident(
            job.server_handle,
            IncidentType.PROVISIONING_FAILED,
            {
                "step": "finalization_replay",
                "reason": disposition.value,
                "episode_id": envelope.finalization.episode_id,
                "identity": envelope.finalization.expected_identity.model_dump(mode="json"),
            },
        )
        result = ProvisionerResult(
            request_id=job.request_id,
            status=(
                ResultStatus.SUPERSEDED
                if disposition is ProvisioningFinalizationDisposition.CONFLICT
                else ResultStatus.FAILED
            ),
            server_handle=job.server_handle,
            server_ip=envelope.finalization.proved_identity.public_ip,
            errors=(
                []
                if disposition is ProvisioningFinalizationDisposition.CONFLICT
                else ["Provisioning finalization was contained"]
            ),
        )
    await _publish_and_ack(client, msg, result)
    await client.redis.delete(_finalization_replay_key(msg.message_id))


async def _handle_stream_message(client, msg) -> None:
    """Handle one new or reclaimed stream delivery through its durable short circuits."""
    job = None
    try:
        job = ProvisionerMessage.model_validate(msg.data)
        state = _decode_hash(await client.redis.hgetall(_outage_key(msg.message_id)))
        if state:
            await _retry_saved_incident(client, msg, job, state)
            return

        replay_key = _finalization_replay_key(msg.message_id)
        try:
            raw_replay = await client.redis.get(replay_key)
        except Exception:
            await _fail_finalization_replay(client, msg, job, "unavailable")
            return
        if raw_replay is not None:
            await _retry_saved_finalization(client, msg, job, raw_replay)
            return
        if msg.reclaimed:
            await _fail_finalization_replay(client, msg, job, "missing_or_expired")
            return

        async def retain(finalization: ProvisioningFinalization) -> None:
            await _retain_finalization(client, msg, job.request_id, job.server_handle, finalization)

        result = await process_provisioner_job(
            job.model_dump(mode="json"),
            retain_finalization=retain,
        )
        await _publish_and_ack(client, msg, result)
        await client.redis.delete(replay_key)
        logger.debug("job_acked", entry_id=msg.message_id)
    except IncidentPersistenceError as error:
        if job is None:
            raise
        await _handle_incident_outage(client, msg, job, error)
    except FinalizationOutcomeUnknown:
        logger.warning("provisioning_finalization_redelivery_pending", entry_id=msg.message_id)
    except Exception as exc:
        logger.error(
            "job_processing_error",
            entry_id=msg.message_id,
            error_type=type(exc).__name__,
        )


async def run_worker():
    """Main worker loop handling provisioning queue."""
    setup_logging(service_name="infra-service")
    validate_provider_policies()
    managed_ids = managed_provider_ids(TIME4VPS_PROVIDER)
    logger.info(
        "provider_policy_validated",
        provider=TIME4VPS_PROVIDER,
        managed_server_count=len(managed_ids),
    )

    client = RedisStreamClient()
    await client.connect()

    logger.info("infrastructure_worker_started", consumer=CONSUMER_NAME)
    try:
        async for msg in client.consume(
            PROVISIONER_QUEUE,
            INFRA_GROUP,
            CONSUMER_NAME,
            auto_ack=False,
            claim_pending=True,
        ):
            if _shutdown:
                break
            if msg is None:
                continue
            await _handle_stream_message(client, msg)
    finally:
        await client.close()
        logger.info("infrastructure_worker_shutdown")


def main():
    """Entry point for running as module."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
