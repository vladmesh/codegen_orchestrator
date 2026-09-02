"""Deploy result handlers — pure run.status/result updates, no story transitions.

Story lifecycle (DEPLOYING → TESTING/FAILED) is managed by the dispatcher's
supervise_deploying_stories(), which reads run.result.deploy_outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from shared.contracts.dto.product_brief import InitialSetting
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.dto.settings_seed import (
    SETTINGS_SEED_RETRYABLE_FAILURES,
    SettingSeedOutcome,
    SettingsSeedFailureKind,
)
from shared.contracts.dto.temporary_access import TemporaryAccessGrantDTO, TemporaryAccessStatus
from shared.contracts.dto.users_grant import (
    GrantIntent,
    GrantIntentKind,
    GrantIntentStatus,
)
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..clients.product_settings import GeneratedServiceSettingsClient
from ..clients.users_grant import GeneratedServiceGrantClient
from ._events import publish_callback_event
from ._live_work import live_work_settled, live_work_unsettled

logger = structlog.get_logger(__name__)

_USERS_GRANT_CAPABILITY = "USERS_GRANT_CAPABILITY"
_SETTINGS_WRITE_CAPABILITY = "SETTINGS_WRITE_CAPABILITY"  # noqa: S105


async def _handle_smoke_failure(
    *,
    result: dict,
    smoke_result: dict,
    task_id: str,
    project_id: str,
    project_name: str,
    callback_stream: str,
    telegram_chat_id: str,
    story_id: str,
    redis: RedisStreamClient,
    msg: DeployMessage,
) -> dict:
    """Handle deploy success with smoke test failure.

    Stores a deterministic retry outcome for the dispatcher to act on.
    """
    smoke_details = "; ".join(
        f"{c['module']}: {c['detail']}"
        for c in smoke_result.get("checks", [])
        if c.get("result") == "fail"
    )
    error_msg = f"Deployed but smoke test failed: {smoke_details}"
    logger.warning(
        "deploy_job_smoke_failed",
        task_id=task_id,
        deployed_url=result["deployed_url"],
        smoke_details=smoke_details,
    )

    run_result = DeployRunResult(
        deploy_outcome=DeployOutcome.RETRY,
        deployed_url=result["deployed_url"],
        deployment_result=result.get("deployment_result"),
        smoke_result=smoke_result,
        error_details=smoke_details,
        deploy_fix_attempt=msg.deploy_fix_attempt,
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
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
    )

    return live_work_unsettled(
        {
            "status": "failed",
            "error": error_msg,
            "deployed_url": result["deployed_url"],
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )


async def _handle_deploy_success(  # noqa: PLR0913
    *,
    result: dict,
    smoke_result: dict | None,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
    callback_stream: str,
    telegram_chat_id: str,
    story_id: str,
    redis: RedisStreamClient,
    application_id: int | None = None,
    grant_intent: GrantIntent | None = None,
    temporary_access_grant: TemporaryAccessGrantDTO | None = None,
    temporary_access_operation: str | None = None,
) -> dict:
    """Handle successful deploy — update run, no story transitions.

    Stores deploy_outcome=success with deployed_url and application_id
    so dispatcher can hand off to QA.
    """
    if grant_intent is not None:
        failure = await _apply_grant_intent(
            task_id=task_id,
            project_id=project_id,
            deployed_url=result["deployed_url"],
            application_id=application_id,
            secret_values=result.get("secret_values", {}),
            intent=grant_intent,
        )
        if failure is not None:
            return await _handle_owner_access_failure(
                result=result,
                task_id=task_id,
                project_id=project_id,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                reason=failure,
                application_id=application_id,
            )

    if temporary_access_grant is not None and temporary_access_operation is not None:
        failure = await _apply_temporary_access_operation(
            task_id=task_id,
            project_id=project_id,
            application_id=application_id,
            secret_values=result.get("secret_values", {}),
            grant=temporary_access_grant,
            operation=temporary_access_operation,
        )
        if failure is not None:
            return await _handle_owner_access_failure(
                result=result,
                task_id=task_id,
                project_id=project_id,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                reason=failure,
                application_id=application_id,
            )

    settings_seed, blocking_seed_failure = await _seed_initial_settings(
        task_id=task_id,
        story_id=story_id,
        deployed_url=result["deployed_url"],
        secret_values=result.get("secret_values", {}),
    )
    if blocking_seed_failure is not None:
        return await _handle_settings_seed_failure(
            result=result,
            task_id=task_id,
            project_id=project_id,
            callback_stream=callback_stream,
            telegram_chat_id=telegram_chat_id,
            redis=redis,
            failure=blocking_seed_failure,
            application_id=application_id,
            settings_seed=settings_seed,
        )

    logger.info(
        "deploy_job_success",
        task_id=task_id,
        deployed_url=result["deployed_url"],
    )
    run_result = DeployRunResult(
        deploy_outcome=DeployOutcome.SUCCESS,
        deployed_url=result["deployed_url"],
        deployment_result=result.get("deployment_result"),
        smoke_result=smoke_result,
        application_id=application_id,
        bot_username=result.get("bot_username"),
        settings_seed=settings_seed,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": run_result.model_dump(mode="json"),
        },
    )

    # Callback for standalone deploys (no story)
    if not story_id:
        await publish_callback_event(
            redis,
            callback_stream,
            "completed",
            task_id,
            f"Deploy completed: {result['deployed_url']}",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id,
        )

    return live_work_settled(
        {
            "status": "success",
            "deployed_url": result["deployed_url"],
            "finished_at": datetime.now(UTC).isoformat(),
        }
    )


async def _apply_grant_intent(  # noqa: PLR0913
    *,
    task_id: str,
    project_id: str,
    deployed_url: str,
    application_id: int | None,
    secret_values: dict,
    intent: GrantIntent,
) -> str | None:
    """Execute exactly one durable intent after deploy health checks.

    The capability comes only from this deploy's in-memory resolver output.
    Every persisted or logged result below is a bounded diagnostic, never the
    capability or a decrypted project secret.
    """
    if intent.status is GrantIntentStatus.APPLIED:
        return None
    if intent.target_application_id is not None and intent.target_application_id != application_id:
        await api_client.complete_users_grant_intent(
            project_id,
            intent.id,
            execution_run_id=task_id,
            active=False,
            detail="target_application_mismatch",
        )
        return "target_application_mismatch"
    capability = secret_values.get(_USERS_GRANT_CAPABILITY)
    if not isinstance(capability, str) or not capability:
        await api_client.complete_users_grant_intent(
            project_id,
            intent.id,
            execution_run_id=task_id,
            active=False,
            detail="capability_unavailable",
        )
        return "capability_unavailable"
    proof = await GeneratedServiceGrantClient(deployed_url).grant_and_resolve(
        channel=intent.channel, external_id=intent.external_id, capability=capability
    )
    if not proof.active:
        safe_failure = proof.failure.value if proof.failure is not None else "unverified"
        await api_client.complete_users_grant_intent(
            project_id, intent.id, execution_run_id=task_id, active=False, detail=safe_failure
        )
        return safe_failure
    try:
        await api_client.complete_users_grant_intent(
            project_id, intent.id, execution_run_id=task_id, active=True
        )
    except Exception:
        if intent.kind is GrantIntentKind.INCOMING_OWNER:
            return "ownership_apply_failed"
        return "intent_apply_failed"
    return None


async def _apply_temporary_access_operation(
    *,
    task_id: str,
    project_id: str,
    application_id: int | None,
    secret_values: dict,
    grant: TemporaryAccessGrantDTO,
    operation: str,
) -> str | None:
    """Use this deploy's capability only while its durable operation is current."""
    if (
        grant.project_id != project_id
        or application_id != grant.target_application_id
        or operation not in {"grant", "revoke"}
    ):
        return "temporary_access_target_mismatch"
    # The grant was read before the deploy began. Recovery can replace it with
    # cleanup while that deploy is still queued or running, so the durable
    # operation transition is re-read immediately before the remote effect.
    # A delayed delivery must never re-grant after a proved revoke.
    current = await api_client.get_temporary_access_grant(grant.id)
    expected_run_id = current.grant_run_id if operation == "grant" else current.revoke_run_id
    expected_status = (
        TemporaryAccessStatus.GRANTING if operation == "grant" else TemporaryAccessStatus.REVOKING
    )
    if current.status is not expected_status or expected_run_id != task_id:
        logger.info(
            "temporary_access_operation_superseded",
            grant_id=grant.id,
            task_id=task_id,
            operation=operation,
            status=current.status.value,
        )
        return "temporary_access_operation_superseded"
    if (
        current.project_id != project_id
        or application_id != current.target_application_id
        or current.head_sha != grant.head_sha
    ):
        return "temporary_access_target_mismatch"
    capability = secret_values.get(_USERS_GRANT_CAPABILITY)
    if not isinstance(capability, str) or not capability:
        return "capability_unavailable"
    client = GeneratedServiceGrantClient(current.target_base_url)
    if operation == "grant":
        proof = await client.grant_and_resolve(
            channel=current.channel, external_id=current.external_id, capability=capability
        )
        if proof.active:
            return None
    else:
        proof = await client.revoke_and_resolve(
            channel=current.channel, external_id=current.external_id, capability=capability
        )
        if not proof.active and proof.failure is None:
            return None
    return proof.failure.value if proof.failure is not None else "unverified"


async def _seed_initial_settings(
    *,
    task_id: str,
    story_id: str,
    deployed_url: str,
    secret_values: dict,
) -> tuple[list[SettingSeedOutcome], SettingsSeedFailureKind | None]:
    """Write the confirmed brief's typed settings into the deployed product.

    Returns `(record, blocking_failure)`. The record is one bounded outcome per
    confirmed setting, in the order the user confirmed them, and it is stored
    on the run whether the deploy is held back or not. A blocking failure is
    returned only for one a second deploy of this commit could answer
    differently — see `SETTINGS_SEED_RETRYABLE_FAILURES`.

    Nothing here is derived from prose, from project config or from an
    environment variable: the values are the confirmed ones, read through the
    released brief endpoint, and the capability is this deploy's in-memory
    resolver output. Neither the capability nor a setting value is logged.
    """
    if not story_id:
        return [], None
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or brief.confirmed_at is None:
        return [], None
    settings = list(brief.content.initial_settings)
    if not settings:
        return [], None

    capability = secret_values.get(_SETTINGS_WRITE_CAPABILITY)
    if not isinstance(capability, str) or not capability:
        # An existing pinned product, generated before the settings core
        # declared its write capability. It seeds nothing and says so; that is
        # not a deploy failure, and no later deploy of the same pin would go
        # any differently.
        logger.info(
            "deploy_settings_seed_capability_unavailable",
            task_id=task_id,
            brief_id=brief.id,
            settings_count=len(settings),
        )
        return [
            _seed_outcome(setting, SettingsSeedFailureKind.CAPABILITY_UNAVAILABLE)
            for setting in settings
        ], None

    proofs = await GeneratedServiceSettingsClient(deployed_url).seed_and_resolve(
        settings, capability=capability
    )
    record = [
        _seed_outcome(setting, None if proof.written else proof.failure)
        for setting, proof in zip(settings, proofs, strict=True)
    ]
    failures = [outcome.failure for outcome in record if outcome.failure is not None]
    logger.info(
        "deploy_settings_seeded",
        task_id=task_id,
        brief_id=brief.id,
        written=sum(1 for outcome in record if outcome.written),
        failures=[failure.value for failure in failures],
    )
    blocking = next(
        (failure for failure in failures if failure in SETTINGS_SEED_RETRYABLE_FAILURES), None
    )
    return record, blocking


def _seed_outcome(
    setting: InitialSetting, failure: SettingsSeedFailureKind | None
) -> SettingSeedOutcome:
    """One setting's disposition, named the way the product identifies it.

    A proof that is neither written nor refused is a defect of the client,
    not a state the run may record: the outcome model refuses it here.
    """
    return SettingSeedOutcome(
        key=setting.key,
        scope=setting.scope,
        subject_id=setting.subject_id,
        written=failure is None,
        failure=failure,
    )


async def _handle_owner_access_failure(  # noqa: PLR0913
    *,
    result: dict,
    task_id: str,
    project_id: str,
    callback_stream: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    reason: str,
    application_id: int | None,
) -> dict:
    """Keep a grant/readback failure retryable without disclosing credentials."""
    error_msg = f"Deployed service did not verify generated access: {reason}"
    logger.warning("deploy_access_proof_failed", task_id=task_id, reason=reason)
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
                deployed_url=result["deployed_url"],
                deployment_result=result.get("deployment_result"),
                smoke_result=result.get("smoke_result"),
                application_id=application_id,
                bot_username=result.get("bot_username"),
                error_details=reason,
            ).model_dump(mode="json"),
        },
    )
    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
    )
    return live_work_unsettled(
        {"status": "failed", "error": error_msg, "deployed_url": result["deployed_url"]}
    )


async def _handle_settings_seed_failure(  # noqa: PLR0913
    *,
    result: dict,
    task_id: str,
    project_id: str,
    callback_stream: str,
    telegram_chat_id: str,
    redis: RedisStreamClient,
    failure: SettingsSeedFailureKind,
    application_id: int | None,
    settings_seed: list[SettingSeedOutcome],
) -> dict:
    """The application is up and a confirmed setting did not arrive in it.

    This has its own outcome rather than borrowing the owner-grant one on
    purpose. `OWNER_ACCESS_PROOF_FAILED` means "the owner grant was not proved",
    and the supervisor reconciles it to SUCCESS as soon as that grant turns out
    to be applied — which on a brief-backed first deploy would hand QA a run
    presented as successful while a confirmed setting the product never
    accepted is only a line in the result nobody routes on.

    The record travels whole: every setting's disposition, the failure kind that
    holds the deploy back, and no value, no capability and no response body.
    """
    error_msg = f"Deployed service did not accept a confirmed setting: {failure.value}"
    logger.warning("deploy_settings_seed_failed", task_id=task_id, failure=failure.value)
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": error_msg,
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.SETTINGS_SEED_FAILED,
                deployed_url=result["deployed_url"],
                deployment_result=result.get("deployment_result"),
                smoke_result=result.get("smoke_result"),
                application_id=application_id,
                bot_username=result.get("bot_username"),
                error_details=f"settings_seed:{failure.value}",
                settings_seed=settings_seed,
            ).model_dump(mode="json"),
        },
    )
    await publish_callback_event(
        redis,
        callback_stream,
        "failed",
        task_id,
        error_msg,
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
    )
    return live_work_unsettled(
        {"status": "failed", "error": error_msg, "deployed_url": result["deployed_url"]}
    )
