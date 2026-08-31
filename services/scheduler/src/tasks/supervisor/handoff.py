"""Durable deploy-to-QA handoff construction and publication."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.project import (
    ProjectDTO,
)
from shared.contracts.dto.qa_handoff import (
    QA_DISPATCHED_AT_KEY,
    QAHandoffPlan,
)
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run_result import (
    DeployRunResult,
)
from shared.queues import (
    QA_QUEUE,
)
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ..temporary_access import grant_temporary_access

logger = structlog.get_logger(__name__)


def _qa_run_id_for_deploy(deploy_run_id: str) -> str:
    """One QA run per deploy run, named so a repeat of the handoff finds it."""
    return f"qa-{deploy_run_id}"[:255]


async def _execute_qa_handoff(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    qa_run_id: str,
    plan: QAHandoffPlan,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Do what the stored plan says, from whichever tick gets there.

    Either the access is recorded — after which the temporary-access sweep owns
    the run and releases it once the value is confirmed deployed — or the message
    goes straight to QA. Both are safe to repeat: the grant id is derived from
    the run, so a second attempt returns the first record, and the publish is
    stamped on the run so it is not repeated once it landed.
    """
    if plan.access is not None:
        grant = await grant_temporary_access(
            api_client,
            redis_client,
            project_id=plan.qa_message.project_id,
            target_application_id=plan.access.target_application_id,
            target_base_url=plan.access.target_base_url,
            head_sha=plan.access.head_sha,
            qa_message=plan.qa_message,
        )
        if grant is None:
            # The contract slot is held by an earlier grant the sweep still owns.
            # Nothing to hand off on this tick; the sweep revokes the holder and
            # a later tick tries again.
            log.info(
                "deploy_supervisor_qa_handoff_deferred_target_held",
                qa_run_id=qa_run_id,
                target_application_id=plan.access.target_application_id,
            )
            return
        log.info(
            "deploy_supervisor_qa_handoff_awaiting_access",
            deployed_url=plan.qa_message.deployed_url,
            qa_run_id=qa_run_id,
            bot_username=plan.qa_message.bot_username,
            grant_id=grant.id,
        )
        return

    await redis_client.publish_message(QA_QUEUE, plan.qa_message)
    await api_client.update_run(
        qa_run_id,
        {"run_metadata": {QA_DISPATCHED_AT_KEY: datetime.now(UTC).isoformat()}},
    )
    log.info(
        "deploy_supervisor_qa_handoff",
        deployed_url=plan.qa_message.deployed_url,
        qa_run_id=qa_run_id,
        bot_username=plan.qa_message.bot_username,
    )


def _temporary_access_is_needed(
    project: ProjectDTO,
    result: DeployRunResult,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Only Telegram bot QA needs the dedicated, revocable QA identity."""
    del result, log
    return "tg_bot" in project.config.get("modules", [])


async def _resolve_qa_repository(
    api_client: SchedulerAPIClient,
    project_id: str,
    log: structlog.stdlib.BoundLogger,
) -> RepositoryDTO | None:
    """Read the repository QA runs against, or None if it can't drive a QA run.

    Carries both the acceptance criteria and the bot username, so the QA handoff
    reads one record instead of two.
    """
    repo = await api_client.get_primary_repository(project_id)
    if repo is None:
        log.error("deploy_success_no_primary_repository")
        return None

    if not (repo.acceptance_criteria or "").strip():
        log.error("deploy_success_no_acceptance_criteria", repo_id=repo.id)
        return None
    return repo
