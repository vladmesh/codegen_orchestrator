"""Deploy Worker — consumes from jobs:deploy queue and runs DevOps.

Pure technical worker: only updates run.status and run.result.
Story lifecycle transitions are handled by the dispatcher.

Run standalone: python -m src.consumers.deploy
"""

from __future__ import annotations

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
from shared.contracts.dto.run_result import DeployRunResult, MissingUserSecret
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
from shared.redis_client import RedisStreamClient

from ..allocations import AllocationError
from ..clients.api import api_client
from ..runtime_identity import project_runtime_slug
from ..subgraphs.devops import create_devops_subgraph
from ._base import start_worker, validate_queued_message
from ._events import publish_callback_event
from ._live_work import live_work_cancel_key, live_work_settled, live_work_unsettled
from .deploy_failure_handler import _handle_deploy_failure
from .deploy_lifecycle import process_lifecycle_action
from .deploy_precheck import (
    SERVICE_BASE_DIR,
    _pre_check_server,
    _run_deploy_precheck,
)
from .deploy_result_handler import (
    _handle_deploy_success,
    _handle_smoke_failure,
)

# Re-export for backward compatibility with tests
__all__ = [
    "SERVICE_BASE_DIR",
    "_build_subgraph_input",
    "_handle_deploy_failure",
    "_handle_deploy_success",
    "_handle_smoke_failure",
    "_pre_check_server",
    "_run_deploy_precheck",
    "process_deploy_job",
]

logger = structlog.get_logger(__name__)

_BOT_AUDIENCE_KEY = "TG_BOT_ALLOWED_TELEGRAM_IDS"
_LEGACY_BOT_AUDIENCE_KEY = "ADMIN_TELEGRAM_ID"

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

    Bot access is project configuration because it is product policy, while QA's
    temporary identity remains a deploy-scoped override. Both are still accepted
    only when the generated repository declares them as contract literals.
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
    bot_access = (project.config or {}).get("bot_access")
    if isinstance(bot_access, dict) and _BOT_AUDIENCE_KEY in message_overrides:
        selected_audience = bot_access.get("allowed_telegram_ids")
        if message_overrides[_BOT_AUDIENCE_KEY] != selected_audience:
            raise ValueError("deploy cannot override the configured bot audience")
    secrets = (project.config or {}).get("secrets", {})
    if (
        isinstance(secrets, dict)
        and _LEGACY_BOT_AUDIENCE_KEY in secrets
        and not isinstance(bot_access, dict)
        and _BOT_AUDIENCE_KEY in message_overrides
    ):
        raise ValueError("deploy cannot override the legacy private bot audience")
    return {**configured, **message_overrides}


def _legacy_bot_audience_needs_contract_resolution(project: ProjectDTO) -> bool:
    """Keep a legacy private bot out of the redundant-deploy shortcut.

    The typed resolver can migrate the encrypted legacy value only after it has
    loaded the generated repository's environment contract. At this point the
    secret key is enough to know that the shortcut must not decide the deploy.
    """
    config = project.config or {}
    secrets = config.get("secrets", {})
    overrides = config.get("env_overrides", {})
    return (
        isinstance(secrets, dict)
        and _LEGACY_BOT_AUDIENCE_KEY in secrets
        and not isinstance(config.get("bot_access"), dict)
        and (not isinstance(overrides, dict) or _BOT_AUDIENCE_KEY not in overrides)
    )


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


async def process_deploy_job(  # noqa: C901, PLR0911, PLR0912, PLR0915
    job_data: dict, redis: RedisStreamClient
) -> dict:
    """Process a single deploy job by running DevOps Subgraph."""
    msg = validate_queued_message(DeployMessage, job_data)
    task_id = msg.task_id
    project_id = msg.project_id
    story_id = msg.story_id
    callback_stream = msg.callback_stream
    telegram_chat_id = msg.telegram_chat_id

    logger.info(
        "deploy_job_started",
        task_id=task_id,
        project_id=project_id,
        triggered_by=msg.triggered_by.value,
    )

    # A run cancelled before this message was picked up is a deploy somebody
    # already gave up on and replaced — the temporary access sweep withdrawing a
    # grant deploy it could not confirm, for one. Its message can outlive the
    # decision in the queue, and running it now would apply an effect after the
    # state that asked for it is gone. Checked before the lock, so refusing does
    # not touch a lock this job never took.
    run = await api_client.get_run(task_id)
    if run.status is RunStatus.CANCELLED:
        logger.info("deploy_job_run_cancelled", task_id=task_id, project_id=project_id)
        return live_work_settled({"status": "cancelled", "reason": "run_cancelled"})

    lock_key = f"deploy:{project_id}:lock"

    try:
        # Atomic Redis lock: only one consumer can process a deploy per project
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
                    # Terminal and typed for the same reason as the fenced case
                    # below: a cancelled run with no outcome is skipped by every
                    # supervisor, so the story it belongs to would wait forever
                    # on a deploy that was never going to run.
                    "result": DeployRunResult(
                        deploy_outcome=DeployOutcome.CANCELLED,
                        action=msg.action,
                    ).model_dump(mode="json"),
                },
            )
            return live_work_unsettled({"status": "cancelled", "reason": "deploy_lock_held"})

        # Take the run to running as one locked decision. The read above is a
        # cheap early-out, not a guard: a withdrawal landing between it and here
        # would be overwritten by a blind patch, and the resurrected run then
        # passes the dispatch claim and deploys the value the withdrawal was
        # revoking. A run cancelled by that point stays cancelled and this job
        # ends instead of starting.
        start = await api_client.start_run(task_id)
        if not start.started:
            logger.info(
                "deploy_job_run_cancelled_before_start",
                task_id=task_id,
                project_id=project_id,
                run_status=start.run_status.value,
            )
            return live_work_settled({"status": "cancelled", "reason": "run_cancelled"})

        # Publish progress event
        await publish_callback_event(
            redis,
            callback_stream,
            "progress",
            task_id,
            "Deploy task started",
            telegram_chat_id=telegram_chat_id,
            project_id=project_id or "",
        )

        if msg.action not in LIFECYCLE_ACTIONS and not msg.head_sha:
            error_msg = "head_sha is required for deploy actions that read repository state"
            logger.error(
                "deploy_head_sha_missing",
                task_id=task_id,
                project_id=project_id,
                action=msg.action.value,
            )
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=error_msg,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                deploy_outcome=DeployOutcome.HEAD_SHA_MISSING,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

        # Fetch project details (with user isolation)
        tg_kwargs = (
            {"telegram_id": int(telegram_chat_id)}
            if telegram_chat_id and telegram_chat_id.isdigit()
            else {}
        )
        project: ProjectDTO | None = await api_client.get_project(project_id, **tg_kwargs)
        if not project:
            error_msg = f"Project {project_id} not found"
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.FAILED.value,
                    "error_message": error_msg,
                    "result": DeployRunResult(deploy_outcome=DeployOutcome.GIVE_UP).model_dump(
                        mode="json"
                    ),
                },
            )
            return live_work_unsettled({"status": "failed", "error": error_msg})

        # Lifecycle actions (stop/undeploy) — skip both allocation and the DevOps
        # subgraph. They bring down an application that already exists; allocating
        # would create one instead of finding the one the message names.
        if msg.action in LIFECYCLE_ACTIONS:
            return await _handle_lifecycle_action(msg, task_id, project_id, project)

        # Get or create allocations for the project
        try:
            alloc_result = await _allocate_resources(project_id, project)
        except AllocationError as error:
            return await _record_infrastructure_wait(task_id, project_id, error)
        if isinstance(alloc_result, str):
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.FAILED.value,
                    "error_message": alloc_result,
                    "result": DeployRunResult(deploy_outcome=DeployOutcome.GIVE_UP).model_dump(
                        mode="json"
                    ),
                },
            )
            return live_work_unsettled({"status": "failed", "error": alloc_result})
        allocated_resources = alloc_result

        try:
            env_overrides = _effective_env_overrides(project, msg.env_overrides)
        except ValueError as error:
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=str(error),
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                deploy_outcome=DeployOutcome.ENVIRONMENT_CONTRACT_INVALID,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

        # A fenced deploy has to run: the shortcut would report a value removed
        # while the run that set it is still live on GitHub Actions.
        application_id = None
        if not msg.fence_active_deploys and not _legacy_bot_audience_needs_contract_resolution(
            project
        ):
            application_id = await _already_deployed_application(
                allocated_resources, msg.head_sha, env_overrides
            )
        if application_id is not None:
            reason = "already_deployed_same_sha"
            logger.info(
                "deploy_redundant_skipped",
                task_id=task_id,
                project_id=project_id,
                application_id=application_id,
                head_sha=msg.head_sha,
                reason=reason,
            )
            await api_client.patch(
                f"runs/{task_id}",
                json={
                    "status": RunStatus.COMPLETED.value,
                    "result": DeployRunResult(
                        deploy_outcome=DeployOutcome.SUCCESS,
                        application_id=application_id,
                        action=msg.action,
                    ).model_dump(mode="json"),
                },
            )
            await publish_callback_event(
                redis,
                callback_stream,
                "completed",
                task_id,
                "Deploy skipped: application already runs this commit",
                telegram_chat_id=telegram_chat_id,
                project_id=project_id,
            )
            return live_work_settled({"status": "success", "reason": reason})

        # Pre-check: validate server state via SSH before deploying
        action = msg.action
        precheck_error = await _run_deploy_precheck(
            allocated_resources, project, project_id, action
        )

        # Auto-fallback: create ↔ feature based on actual server state
        if precheck_error and action == "create" and "already exists" in precheck_error:
            logger.warning(
                "deploy_action_auto_fallback",
                task_id=task_id,
                from_action="create",
                to_action="feature",
                reason=precheck_error,
            )
            action = "feature"
            precheck_error = await _run_deploy_precheck(
                allocated_resources, project, project_id, action
            )
        if precheck_error:
            logger.warning("deploy_precheck_failed", task_id=task_id, error=precheck_error)
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=precheck_error,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

        # Resolve git_url from primary Repository entity
        primary_repo = await api_client.get_primary_repository(project_id)
        _git_url = primary_repo.git_url if primary_repo else ""

        # Run DevOps subgraph
        devops_subgraph = create_devops_subgraph()
        subgraph_input = _build_subgraph_input(
            project_id,
            project,
            _git_url,
            allocated_resources,
            job_data,
            head_sha=msg.head_sha,
            fence_active_deploys=msg.fence_active_deploys,
        )
        result = await devops_subgraph.ainvoke(subgraph_input)

        logger.info(
            "devops_subgraph_result",
            task_id=task_id,
            result_keys=sorted(result.keys()),
            has_smoke_result="smoke_result" in result,
            smoke_result=result.get("smoke_result"),
            deployed_url=result.get("deployed_url"),
            errors=result.get("errors"),
        )

        if result.get("deployment_result", {}).get("status") == "cancelled":
            # A cancelled deploy is terminal, and the run has to say so. Left at
            # RUNNING it is skipped by every supervisor for good, and the story
            # behind it waits on a deploy nobody is carrying any more. The fence
            # a revoke takes cancels ordinary deploys as a matter of course, so
            # this is a normal path, not a teardown corner.
            logger.info("deploy_job_cancelled_during_actions", task_id=task_id)
            await api_client.patch(
                f"runs/{task_id}",
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
            smoke_failed = smoke_result and smoke_result.get("status") == "fail"

            if smoke_failed:
                project_name = project_runtime_slug(project)
                return await _handle_smoke_failure(
                    result=result,
                    smoke_result=smoke_result,
                    task_id=task_id,
                    project_id=project_id,
                    project_name=project_name,
                    callback_stream=callback_stream,
                    telegram_chat_id=telegram_chat_id,
                    story_id=story_id,
                    redis=redis,
                    msg=msg,
                )

            return await _handle_deploy_success(
                result=result,
                smoke_result=smoke_result,
                task_id=task_id,
                project_id=project_id,
                project=project,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                story_id=story_id,
                redis=redis,
                application_id=result.get("application_id"),
            )
        elif result.get("missing_user_secrets"):
            missing = [
                MissingUserSecret.model_validate(entry)
                for entry in result.get("missing_user_secrets")
            ]
            missing_keys = [m.key for m in missing]
            logger.info("deploy_job_missing_secrets", task_id=task_id, missing=missing_keys)
            typed_outcome = _resolution_outcome(result)
            if typed_outcome is not None and typed_outcome != DeployOutcome.WAITING_FOR_USER_SECRET:
                raise ValueError(
                    "missing_user_secrets present but resolution_outcome is "
                    f"{typed_outcome}, expected {DeployOutcome.WAITING_FOR_USER_SECRET}"
                )
            outcome = DeployOutcome.WAITING_FOR_USER_SECRET
            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=f"Missing secrets: {', '.join(missing_keys)}",
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                deploy_outcome=outcome,
                deploy_fix_attempt=msg.deploy_fix_attempt,
                missing_user_secrets=missing,
            )
        else:
            typed_outcome = _resolution_outcome(result)
            if typed_outcome:
                errors = result.get("errors", ["Environment resolution failed"])
                return await _handle_deploy_failure(
                    task_id=task_id,
                    project_id=project_id,
                    story_id=story_id,
                    error_msg="; ".join(errors),
                    callback_stream=callback_stream,
                    telegram_chat_id=telegram_chat_id,
                    redis=redis,
                    deploy_outcome=typed_outcome,
                    deploy_fix_attempt=msg.deploy_fix_attempt,
                )
            errors = result.get("errors", ["Unknown deployment error"])
            logger.error("deploy_job_failed", task_id=task_id, errors=errors)
            error_msg = "; ".join(errors)

            return await _handle_deploy_failure(
                task_id=task_id,
                project_id=project_id,
                story_id=story_id,
                error_msg=error_msg,
                callback_stream=callback_stream,
                telegram_chat_id=telegram_chat_id,
                redis=redis,
                deploy_outcome=DeployOutcome.RETRY,
                deploy_fix_attempt=msg.deploy_fix_attempt,
            )

    except WorkflowCancellationUnprovenError:
        # Teardown could not prove the dispatched GitHub Actions run stopped.
        # Masking this as a normal deploy failure would ACK the queue entry and
        # let cleanup delete external/DB resources while the run may still execute.
        # Propagate so the live-work fence marks the failure and cleanup fails closed.
        logger.error(
            "deploy_workflow_cancellation_unproven",
            task_id=task_id,
            project_id=project_id,
        )
        raise
    except Exception as e:
        if project_id and await redis.redis.exists(live_work_cancel_key(project_id)):
            # Live teardown is fencing this project, so no deploy-path failure is
            # normal: the dispatched deploy.yml run may still be executing. Handling
            # it as a deploy failure would ACK the queue entry and let cleanup delete
            # external and DB resources. Propagate to the live-work fence instead.
            logger.error(
                "deploy_job_exception_under_live_teardown",
                task_id=task_id,
                project_id=project_id,
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise
        logger.error(
            "deploy_job_exception",
            task_id=task_id,
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return await _handle_deploy_failure(
            task_id=task_id,
            project_id=project_id,
            story_id=story_id,
            error_msg=str(e),
            callback_stream=callback_stream,
            telegram_chat_id=telegram_chat_id,
            redis=redis,
            deploy_fix_attempt=msg.deploy_fix_attempt,
        )
    finally:
        # Always release the deploy lock so the next deploy can proceed
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
