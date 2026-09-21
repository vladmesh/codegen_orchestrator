"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Pure technical worker: only updates run.status and run.result.
Story lifecycle transitions are handled by the dispatcher.

Run standalone: python -m src.consumers.deploy
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from shared.allocation_disposition import attempt_disposition, may_terminate_story
from shared.clients.github import WorkflowCancellationUnprovenError
from shared.config_store import ConfigStore
from shared.contracts.dto.application import (
    DEFAULT_APPLICATION_RESERVED_RAM_MB,
    ApplicationStatus,
)
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import DeployRunResult, DeploySkipReason, MissingUserSecret
from shared.contracts.dto.users_grant import USERS_GRANT_INTENT_KEY
from shared.contracts.env_overrides import (
    EMPTY_OVERRIDES_DIGEST,
    env_overrides_digest,
)
from shared.contracts.queues.deploy import (
    LIFECYCLE_ACTIONS,
    DeployAction,
    DeployMessage,
    DeployOutcome,
)
from shared.contracts.service_ports import DEPLOY_INFRA_PORT_SERVICES
from shared.queues import DEPLOY_QUEUE
from shared.redis import RedisStreamClient

from ..allocations import AllocationError
from ..clients.api import api_client
from ..runtime_identity import project_runtime_slug
from ..subgraphs.devops import create_devops_subgraph
from ._base import start_worker, validate_queued_message
from ._events import publish_callback_event
from ._live_work import live_work_cancel_key, live_work_settled, live_work_unsettled
from .deploy_failure_handler import _handle_deploy_failure
from .deploy_lifecycle import process_lifecycle_action
from .deploy_precheck import _run_deploy_precheck
from .deploy_result_handler import (
    _handle_deploy_success,
    _handle_smoke_failure,
)

logger = structlog.get_logger(__name__)

_config: ConfigStore | None = None


def _deploy_lock_ttl() -> int:
    global _config  # noqa: PLW0603
    if _config is None:
        import os

        api_base_url = os.getenv("API_BASE_URL")
        if not api_base_url:
            raise RuntimeError("API_BASE_URL is not set")
        _config = ConfigStore(api_base_url)
    return _config.get_int("deploy.deploy_lock_ttl", default=3600)


async def _allocate_resources(project_id: str, project: ProjectDTO) -> dict | str:
    """Get or create allocations. Returns dict of resources or error string.

    An `AllocationError` is deliberately *not* caught here. It is the one failure
    on this path that is about the platform rather than the project, and it
    carries the classification the scheduler needs; flattening it into this
    function's error string is exactly how an unfinished host build used to reach
    the story as a product failure. The caller handles it as a typed outcome.
    """
    from ..allocations import ensure_project_allocations

    config = project.config or {}
    modules = list(
        dict.fromkeys([*config.get("modules", ["backend"]), *DEPLOY_INFRA_PORT_SERVICES])
    )
    min_ram_mb = config.get("estimated_ram_mb", DEFAULT_APPLICATION_RESERVED_RAM_MB)

    # Get repo_id from primary repository
    primary_repo = await api_client.get_primary_repository(project_id)
    if not primary_repo:
        return f"No repository found for project {project_id}"
    repo_id = primary_repo.id
    service_name = project_runtime_slug(project)

    return await ensure_project_allocations(
        project_id=project_id,
        repo_id=repo_id,
        service_name=service_name,
        modules=modules,
        min_ram_mb=min_ram_mb,
    )


async def _record_infrastructure_wait(
    task_id: str, project_id: str, error: AllocationError
) -> dict:
    """Record a deploy that could not be placed, without blaming the project.

    The disposition comes from `shared.allocation_disposition`, the same place the
    engineering path asks; this consumer keeps no list of its own. Every
    allocation refusal classifies as infrastructure there, so this run never
    records GIVE_UP — the outcome the scheduler turns into a failed story and an
    admin product-failure alert. A refusal that ever classified as a product
    failure would be a defect in that table, and it is refused loudly here rather
    than quietly routed as one.
    """
    disposition = attempt_disposition(error.reason, product_failure=True)
    if may_terminate_story(disposition):
        raise AssertionError(
            f"allocation refusal {error.reason.value} classified as {disposition.value}"
        )
    logger.warning(
        "deploy_allocation_infrastructure_wait",
        task_id=task_id,
        project_id=project_id,
        reason=error.reason.value,
        disposition=disposition.value,
        required_ram_mb=error.required_ram_mb,
        min_disk_mb=error.min_disk_mb,
    )
    await api_client.patch(
        f"runs/{task_id}",
        json={
            "status": RunStatus.FAILED.value,
            "error_message": str(error),
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.WAITING_INFRASTRUCTURE,
                allocation_failure_reason=error.reason,
                allocation_required_ram_mb=error.required_ram_mb,
                allocation_min_disk_mb=error.min_disk_mb,
                error_details=str(error),
            ).model_dump(mode="json"),
        },
    )
    return live_work_unsettled({"status": "waiting_infrastructure", "error": str(error)})


def _resolution_outcome(result: dict) -> DeployOutcome | None:
    """Read the outcome the DevOps subgraph set, refusing an untyped stand-in.

    The subgraph nodes set `DeployOutcome` members. Accepting a bare string here
    would re-open the reverse-parse path where an outcome the consumer does not
    recognise collapses into a generic failure and loses its dispatcher routing.
    """
    outcome = result.get("resolution_outcome")
    if outcome is None or isinstance(outcome, DeployOutcome):
        return outcome
    raise TypeError(
        f"resolution_outcome must be a DeployOutcome, got {type(outcome).__name__}: {outcome!r}"
    )


def _build_subgraph_input(
    project_id: str,
    project: ProjectDTO,
    git_url: str,
    allocated_resources: dict,
    job_data: dict,
    head_sha: str,
    deployed_commit_sha: str,
    fence_active_deploys: bool,
) -> dict:
    """Build DevOps subgraph input from deploy job data."""
    if not head_sha:
        raise ValueError("head_sha is required to build DevOps subgraph input")
    return {
        "project_id": project_id,
        "run_id": job_data.get("task_id"),
        "project_spec": project.model_dump(),
        "repo_info": {
            "full_name": git_url.replace("https://github.com/", "")
            .rstrip("/")
            .removesuffix(".git"),
            "html_url": git_url,
        },
        "allocated_resources": allocated_resources,
        "provided_secrets": job_data.get("provided_secrets", {}),
        "env_overrides": _effective_env_overrides(project, job_data.get("env_overrides", {})),
        "head_sha": head_sha,
        "deployed_commit_sha": deployed_commit_sha,
        "fence_active_deploys": fence_active_deploys,
        "messages": [],
        "environment_contract": None,
        "resolution_outcome": None,
        "secret_values": {},
        "non_secret_values": {},
        "missing_user_secrets": [],
        "deployment_result": None,
        "deployed_url": None,
        "smoke_result": None,
        "errors": [],
    }


def _effective_env_overrides(project: ProjectDTO, message_overrides: dict | None) -> dict[str, str]:
    """Combine persisted project literals with per-deploy literals.

    Only explicitly declared non-secret literals can be supplied this way.
    """
    configured = (project.config or {}).get("env_overrides", {})
    if not isinstance(configured, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in configured.items()
    ):
        raise ValueError("project env_overrides must be a string mapping")
    if not isinstance(message_overrides, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in message_overrides.items()
    ):
        raise ValueError("deploy env_overrides must be a string mapping")
    return {**configured, **message_overrides}


async def _already_deployed_application(
    allocated_resources: dict, head_sha: str, env_overrides: dict[str, str] | None = None
) -> int | None:
    """Return a running application already deployed from this commit and environment.

    The commit alone does not identify a deploy: the same commit with different
    deploy-time environment is a different deploy, and treating it as redundant
    would silently drop the change — including a redeploy whose whole purpose is to
    remove a value. Records written before the digest existed compare equal to a
    deploy that sets nothing, which is what they were.
    """
    application_ids = {
        resource["application_id"]
        for resource in allocated_resources.values()
        if isinstance(resource, dict) and resource.get("application_id") is not None
    }
    for application_id in application_ids:
        deployments = await api_client.get(
            "service-deployments/",
            params={"application_id": application_id, "result": "success"},
        )
        if not deployments:
            continue

        latest_deployment = deployments[0]
        if latest_deployment.get("deployed_sha") != head_sha:
            continue

        recorded = (latest_deployment.get("deployment_info") or {}).get(
            "env_overrides_digest", EMPTY_OVERRIDES_DIGEST
        )
        if recorded != env_overrides_digest(env_overrides):
            continue

        application = await api_client.get_application(application_id)
        if application.status == ApplicationStatus.RUNNING:
            return application_id
    return None


async def _handle_lifecycle_action(
    msg: DeployMessage,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
) -> dict:
    """Handle stop/undeploy lifecycle actions — SSH only, no DevOps subgraph.

    The target comes from the message. Asking the allocator instead would answer
    with whichever application it picks for the project's primary repository, so a
    project deployed on two servers would get the same container stopped twice
    while the other one keeps running.
    """
    application = await api_client.get_application(msg.application_id)
    project_name = project_runtime_slug(project)
    lifecycle_result = await process_lifecycle_action(
        action=msg.action,
        task_id=task_id,
        project_id=project_id,
        project_name=project_name,
        server_handle=application.server_handle,
    )
    run_status = (
        RunStatus.COMPLETED if lifecycle_result["status"] == "success" else RunStatus.FAILED
    )
    run_result = DeployRunResult(
        deploy_outcome=lifecycle_result["deploy_outcome"],
        action=msg.action,
    )
    run_patch: dict = {
        "status": run_status.value,
        "result": run_result.model_dump(mode="json"),
    }
    if lifecycle_result.get("error"):
        run_patch["error_message"] = lifecycle_result["error"]
    await api_client.patch(f"runs/{task_id}", json=run_patch)

    # Update application status on success
    if lifecycle_result["status"] == "success":
        app_id = msg.application_id
        target_status = (
            ApplicationStatus.NOT_DEPLOYED
            if msg.action == DeployAction.UNDEPLOY
            else ApplicationStatus.STOPPED
        )
        await api_client.patch(
            f"applications/{app_id}",
            json={"status": target_status.value},
        )

    return lifecycle_result


@dataclass(frozen=True)
class DeployAccessContext:
    """Validated access capabilities carried into one deploy execution."""

    grant_intent: Any | None = None
    temporary_access_grant: Any | None = None
    temporary_access_operation: str | None = None


@dataclass(frozen=True)
class DeployBaseContext:
    """Project and access facts required before resource preparation."""

    project: ProjectDTO
    access: DeployAccessContext


@dataclass(frozen=True)
class PreparedDeploy:
    """Inputs that are safe to hand to the DevOps subgraph."""

    base: DeployBaseContext
    subgraph_input: dict


@dataclass(frozen=True)
class DeployTerminal:
    """A deploy path that already persisted and classified its terminal response."""

    response: dict


async def _deploy_failure_terminal(
    msg: DeployMessage,
    redis: RedisStreamClient,
    error_msg: str,
    *,
    deploy_outcome: DeployOutcome = DeployOutcome.RETRY,
    missing_user_secrets: list[MissingUserSecret] | None = None,
) -> DeployTerminal:
    """Persist one classified deploy failure and wrap its worker response."""
    response = await _handle_deploy_failure(
        task_id=msg.task_id,
        project_id=msg.project_id,
        story_id=msg.story_id,
        error_msg=error_msg,
        callback_stream=msg.callback_stream,
        telegram_chat_id=msg.telegram_chat_id,
        redis=redis,
        deploy_outcome=deploy_outcome,
        deploy_fix_attempt=msg.deploy_fix_attempt,
        missing_user_secrets=missing_user_secrets,
    )
    return DeployTerminal(response)


async def _claim_deploy_job(
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> DeployTerminal | None:
    """Acquire the project deploy lock and atomically move the run to RUNNING."""
    task_id = msg.task_id
    project_id = msg.project_id
    lock_key = f"deploy:{project_id}:lock"

    acquired = await redis.redis.set(lock_key, task_id, nx=True, ex=_deploy_lock_ttl())
    if not acquired:
        logger.info(
            "deploy_lock_not_acquired",
            task_id=task_id,
            project_id=project_id,
            lock_key=lock_key,
        )
        await api_client.patch(
            f"runs/{task_id}",
            json={
                "status": RunStatus.CANCELLED.value,
                "error_message": (
                    f"Skipped: another deploy is already in progress for project {project_id}"
                ),
                "result": DeployRunResult(
                    deploy_outcome=DeployOutcome.CANCELLED,
                    action=msg.action,
                ).model_dump(mode="json"),
            },
        )
        return DeployTerminal(
            live_work_unsettled({"status": "cancelled", "reason": "deploy_lock_held"})
        )

    start = await api_client.start_run(task_id)
    if not start.started:
        logger.info(
            "deploy_job_run_cancelled_before_start",
            task_id=task_id,
            project_id=project_id,
            run_status=start.run_status.value,
        )
        return DeployTerminal(live_work_settled({"status": "cancelled", "reason": "run_cancelled"}))

    return None


async def _resolve_deploy_access(
    run: Any,
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> DeployAccessContext | DeployTerminal:
    """Validate durable grant references before any deploy side effect is attempted."""
    grant_intent = None
    stored_intent = (getattr(run, "run_metadata", None) or {}).get(USERS_GRANT_INTENT_KEY)
    if stored_intent is not None:
        try:
            if not isinstance(stored_intent, str):
                raise ValueError("grant intent reference is not a string")
            grant_intent = await api_client.get_users_grant_intent(msg.project_id, stored_intent)
        except (TypeError, ValueError):
            return await _deploy_failure_terminal(
                msg,
                redis,
                "grant intent is malformed",
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
            )
        if (
            grant_intent.project_id != msg.project_id
            or grant_intent.target_sha != msg.head_sha
            or grant_intent.execution_run_id != msg.task_id
        ):
            return await _deploy_failure_terminal(
                msg,
                redis,
                "grant intent target does not match deploy message",
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
            )

    temporary_access_grant = None
    temporary_access_operation = None
    metadata = getattr(run, "run_metadata", None) or {}
    stored_temporary_access_grant = metadata.get("temporary_access_grant_id")
    if stored_temporary_access_grant is not None:
        temporary_access_operation = metadata.get("temporary_access_operation")
        if not isinstance(stored_temporary_access_grant, str) or not isinstance(
            temporary_access_operation, str
        ):
            return await _deploy_failure_terminal(
                msg,
                redis,
                "temporary access operation is malformed",
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
            )
        temporary_access_grant = await api_client.get_temporary_access_grant(
            stored_temporary_access_grant
        )
        if (
            temporary_access_grant.project_id != msg.project_id
            or temporary_access_grant.head_sha != msg.head_sha
            or temporary_access_operation not in {"grant", "revoke"}
        ):
            return await _deploy_failure_terminal(
                msg,
                redis,
                "temporary access target does not match deploy message",
                deploy_outcome=DeployOutcome.OWNER_ACCESS_PROOF_FAILED,
            )

    return DeployAccessContext(
        grant_intent=grant_intent,
        temporary_access_grant=temporary_access_grant,
        temporary_access_operation=temporary_access_operation,
    )


async def _load_deploy_base(
    run: Any,
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> DeployBaseContext | DeployTerminal:
    """Validate message/project/access facts that precede resource preparation."""
    if msg.action not in LIFECYCLE_ACTIONS and not msg.head_sha:
        error_msg = "head_sha is required for deploy actions that read repository state"
        logger.error(
            "deploy_head_sha_missing",
            task_id=msg.task_id,
            project_id=msg.project_id,
            action=msg.action.value,
        )
        return await _deploy_failure_terminal(
            msg,
            redis,
            error_msg,
            deploy_outcome=DeployOutcome.HEAD_SHA_MISSING,
        )

    tg_kwargs = (
        {"telegram_id": int(msg.telegram_chat_id)}
        if msg.telegram_chat_id and msg.telegram_chat_id.isdigit()
        else {}
    )
    project: ProjectDTO | None = await api_client.get_project(msg.project_id, **tg_kwargs)
    if not project:
        error_msg = f"Project {msg.project_id} not found"
        await api_client.patch(
            f"runs/{msg.task_id}",
            json={
                "status": RunStatus.FAILED.value,
                "error_message": error_msg,
                "result": DeployRunResult(deploy_outcome=DeployOutcome.GIVE_UP).model_dump(
                    mode="json"
                ),
            },
        )
        return DeployTerminal(live_work_unsettled({"status": "failed", "error": error_msg}))

    access = await _resolve_deploy_access(run, msg, redis)
    if isinstance(access, DeployTerminal):
        return access

    if msg.action in LIFECYCLE_ACTIONS:
        return DeployTerminal(
            await _handle_lifecycle_action(msg, msg.task_id, msg.project_id, project)
        )

    return DeployBaseContext(project=project, access=access)


async def _allocate_deploy_resources(
    base: DeployBaseContext,
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> tuple[dict, dict[str, str]] | DeployTerminal:
    """Resolve placement and effective environment for a normal deploy."""
    try:
        alloc_result = await _allocate_resources(msg.project_id, base.project)
    except AllocationError as error:
        return DeployTerminal(await _record_infrastructure_wait(msg.task_id, msg.project_id, error))

    if isinstance(alloc_result, str):
        await api_client.patch(
            f"runs/{msg.task_id}",
            json={
                "status": RunStatus.FAILED.value,
                "error_message": alloc_result,
                "result": DeployRunResult(
                    deploy_outcome=DeployOutcome.GIVE_UP
                ).model_dump(mode="json"),
            },
        )
        return DeployTerminal(live_work_unsettled({"status": "failed", "error": alloc_result}))

    try:
        env_overrides = _effective_env_overrides(base.project, msg.env_overrides)
    except ValueError as error:
        return await _deploy_failure_terminal(
            msg,
            redis,
            str(error),
            deploy_outcome=DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
        )
    return alloc_result, env_overrides


async def _maybe_skip_redundant_deploy(
    base: DeployBaseContext,
    msg: DeployMessage,
    redis: RedisStreamClient,
    allocated_resources: dict,
    env_overrides: dict[str, str],
) -> DeployTerminal | None:
    """Complete a deploy immediately when its exact commit/environment is already live."""
    access = base.access
    if (
        access.grant_intent is not None
        or access.temporary_access_grant is not None
        or msg.fence_active_deploys
    ):
        return None

    application_id = await _already_deployed_application(
        allocated_resources, msg.head_sha, env_overrides
    )
    if application_id is None:
        return None

    reason = DeploySkipReason.ALREADY_DEPLOYED_SAME_SHA
    logger.info(
        "deploy_redundant_skipped",
        task_id=msg.task_id,
        project_id=msg.project_id,
        application_id=application_id,
        head_sha=msg.head_sha,
        reason=reason.value,
    )
    await api_client.patch(
        f"runs/{msg.task_id}",
        json={
            "status": RunStatus.COMPLETED.value,
            "result": DeployRunResult(
                deploy_outcome=DeployOutcome.SUCCESS,
                application_id=application_id,
                action=msg.action,
                skipped_reason=reason,
            ).model_dump(mode="json"),
        },
    )
    await publish_callback_event(
        redis,
        msg.callback_stream,
        "completed",
        msg.task_id,
        "Deploy skipped: application already runs this commit",
        telegram_chat_id=msg.telegram_chat_id,
        project_id=msg.project_id,
    )
    return DeployTerminal(live_work_settled({"status": "success", "reason": reason.value}))


async def _precheck_deploy(
    base: DeployBaseContext,
    msg: DeployMessage,
    redis: RedisStreamClient,
    allocated_resources: dict,
) -> DeployTerminal | None:
    """Run the deploy pre-check, including the existing create→feature probe fallback."""
    action = msg.action
    precheck_error = await _run_deploy_precheck(
        allocated_resources, base.project, msg.project_id, action
    )
    if precheck_error and action == "create" and "already exists" in precheck_error:
        logger.warning(
            "deploy_action_auto_fallback",
            task_id=msg.task_id,
            from_action="create",
            to_action="feature",
            reason=precheck_error,
        )
        precheck_error = await _run_deploy_precheck(
            allocated_resources, base.project, msg.project_id, "feature"
        )
    if not precheck_error:
        return None

    logger.warning("deploy_precheck_failed", task_id=msg.task_id, error=precheck_error)
    return await _deploy_failure_terminal(msg, redis, precheck_error)


async def _prepare_deploy(
    base: DeployBaseContext,
    msg: DeployMessage,
    job_data: dict,
    redis: RedisStreamClient,
) -> PreparedDeploy | DeployTerminal:
    """Turn validated project facts into one safe DevOps-subgraph invocation."""
    resources = await _allocate_deploy_resources(base, msg, redis)
    if isinstance(resources, DeployTerminal):
        return resources
    allocated_resources, env_overrides = resources

    redundant = await _maybe_skip_redundant_deploy(
        base,
        msg,
        redis,
        allocated_resources,
        env_overrides,
    )
    if redundant is not None:
        return redundant

    precheck = await _precheck_deploy(base, msg, redis, allocated_resources)
    if precheck is not None:
        return precheck

    primary_repo = await api_client.get_primary_repository(msg.project_id)
    git_url = primary_repo.git_url if primary_repo else ""
    return PreparedDeploy(
        base=base,
        subgraph_input=_build_subgraph_input(
            msg.project_id,
            base.project,
            git_url,
            allocated_resources,
            job_data,
            head_sha=msg.head_sha,
            deployed_commit_sha=msg.deployed_commit_sha,
            fence_active_deploys=msg.fence_active_deploys,
        ),
    )


async def _route_deploy_result(
    result: dict,
    prepared: PreparedDeploy,
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> dict:
    """Map one DevOps-subgraph result to the deploy worker's durable typed outcome."""
    if result.get("deployment_result", {}).get("status") == "cancelled":
        logger.info("deploy_job_cancelled_during_actions", task_id=msg.task_id)
        await api_client.patch(
            f"runs/{msg.task_id}",
            json={
                "status": RunStatus.CANCELLED.value,
                "error_message": "Deploy was cancelled before it could finish",
                "result": DeployRunResult(
                    deploy_outcome=DeployOutcome.CANCELLED,
                    action=msg.action,
                    deployment_result=result.get("deployment_result"),
                ).model_dump(mode="json"),
            },
        )
        return live_work_unsettled({"status": "cancelled"})

    if result.get("deployed_url"):
        smoke_result = result.get("smoke_result")
        if smoke_result and smoke_result.get("status") == "fail":
            return await _handle_smoke_failure(
                result=result,
                smoke_result=smoke_result,
                task_id=msg.task_id,
                project_id=msg.project_id,
                project_name=project_runtime_slug(prepared.base.project),
                callback_stream=msg.callback_stream,
                telegram_chat_id=msg.telegram_chat_id,
                story_id=msg.story_id,
                redis=redis,
                msg=msg,
            )
        access = prepared.base.access
        return await _handle_deploy_success(
            result=result,
            smoke_result=smoke_result,
            task_id=msg.task_id,
            project_id=msg.project_id,
            project=prepared.base.project,
            callback_stream=msg.callback_stream,
            telegram_chat_id=msg.telegram_chat_id,
            story_id=msg.story_id,
            redis=redis,
            msg=msg,
            application_id=result.get("application_id"),
            grant_intent=access.grant_intent,
            temporary_access_grant=access.temporary_access_grant,
            temporary_access_operation=access.temporary_access_operation,
        )

    if result.get("missing_user_secrets"):
        missing = [
            MissingUserSecret.model_validate(entry) for entry in result.get("missing_user_secrets")
        ]
        missing_keys = [secret.key for secret in missing]
        logger.info("deploy_job_missing_secrets", task_id=msg.task_id, missing=missing_keys)
        typed_outcome = _resolution_outcome(result)
        if typed_outcome is not None and typed_outcome != DeployOutcome.WAITING_FOR_USER_SECRET:
            raise ValueError(
                "missing_user_secrets present but resolution_outcome is "
                f"{typed_outcome}, expected {DeployOutcome.WAITING_FOR_USER_SECRET}"
            )
        return (
            await _deploy_failure_terminal(
                msg,
                redis,
                f"Missing secrets: {', '.join(missing_keys)}",
                deploy_outcome=DeployOutcome.WAITING_FOR_USER_SECRET,
                missing_user_secrets=missing,
            )
        ).response

    typed_outcome = _resolution_outcome(result)
    if typed_outcome:
        errors = result.get("errors", ["Environment resolution failed"])
        return (
            await _deploy_failure_terminal(
                msg,
                redis,
                "; ".join(errors),
                deploy_outcome=typed_outcome,
            )
        ).response

    errors = result.get("errors", ["Unknown deployment error"])
    logger.error("deploy_job_failed", task_id=msg.task_id, errors=errors)
    return (
        await _deploy_failure_terminal(
            msg,
            redis,
            "; ".join(errors),
            deploy_outcome=DeployOutcome.RETRY,
        )
    ).response


async def _execute_prepared_deploy(
    prepared: PreparedDeploy,
    msg: DeployMessage,
    redis: RedisStreamClient,
) -> dict:
    """Invoke DevOps once and route its result through the closed result dispatcher."""
    result = await create_devops_subgraph().ainvoke(prepared.subgraph_input)
    logger.info(
        "devops_subgraph_result",
        task_id=msg.task_id,
        result_keys=sorted(result.keys()),
        has_smoke_result="smoke_result" in result,
        smoke_result=result.get("smoke_result"),
        deployed_url=result.get("deployed_url"),
        errors=result.get("errors"),
    )
    return await _route_deploy_result(result, prepared, msg, redis)


async def process_deploy_job(job_data: dict, redis: RedisStreamClient) -> dict:
    """Process a single deploy job through explicit claim, prepare and result phases."""
    msg = validate_queued_message(DeployMessage, job_data)
    task_id = msg.task_id
    project_id = msg.project_id

    logger.info(
        "deploy_job_started",
        task_id=task_id,
        project_id=project_id,
        triggered_by=msg.triggered_by.value,
    )

    run = await api_client.get_run(task_id)
    if run.status is RunStatus.CANCELLED:
        logger.info("deploy_job_run_cancelled", task_id=task_id, project_id=project_id)
        return live_work_settled({"status": "cancelled", "reason": "run_cancelled"})

    lock_key = f"deploy:{project_id}:lock"
    try:
        claimed = await _claim_deploy_job(msg, redis)
        if claimed is not None:
            return claimed.response

        await publish_callback_event(
            redis,
            msg.callback_stream,
            "progress",
            task_id,
            "Deploy task started",
            telegram_chat_id=msg.telegram_chat_id,
            project_id=project_id or "",
        )

        base = await _load_deploy_base(run, msg, redis)
        if isinstance(base, DeployTerminal):
            return base.response

        prepared = await _prepare_deploy(base, msg, job_data, redis)
        if isinstance(prepared, DeployTerminal):
            return prepared.response

        return await _execute_prepared_deploy(prepared, msg, redis)

    except WorkflowCancellationUnprovenError:
        logger.error(
            "deploy_workflow_cancellation_unproven",
            task_id=task_id,
            project_id=project_id,
        )
        raise
    except Exception as error:
        if project_id and await redis.redis.exists(live_work_cancel_key(project_id)):
            logger.error(
                "deploy_job_exception_under_live_teardown",
                task_id=task_id,
                project_id=project_id,
                error_type=type(error).__name__,
                exc_info=True,
            )
            raise
        logger.error(
            "deploy_job_exception",
            task_id=task_id,
            error=str(error),
            error_type=type(error).__name__,
            exc_info=True,
        )
        return (await _deploy_failure_terminal(msg, redis, str(error))).response
    finally:
        await redis.redis.delete(lock_key)


def main():
    """Entry point for running as module."""
    start_worker(
        service_name="deploy-worker",
        queue=DEPLOY_QUEUE,
        process_fn=process_deploy_job,
    )


if __name__ == "__main__":
    main()
