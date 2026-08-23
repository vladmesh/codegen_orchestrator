"""Bot-audience mutations and their configuration-only live rollouts.

A Telegram audience change on a project is one of two things, decided by
whether the bot is running:

* nothing is deployed — the write is the whole effect, and the response says
  so (`rollout: not_deployed`);
* a bot is running — the write is only half the effect, and the other half is
  a redeploy of *exactly the commit already live*, which re-reads this
  project's `env_overrides` in the DevOps subgraph and ships the new audience.
  No story, no engineering, no rebuild, no CI.

Every mutation — `set_bot_access`'s whole-audience selection and the typed
one-ID add/remove — goes through `_mutate_bot_audience`, so authorization,
row-lock atomicity, the final-ID guard, idempotency, the publish-intent record
and the rollout staging are written once.

The publish-intent record (`BotRolloutRecord`) is the seam that closes the
commit/publish gap: it is committed with the run row *before* anything is
published, and a scheduler sweep retries publishing from it until the stream
accepts or attempts run out. A retry that finds an unchanged-but-unapplied
audience resumes that work instead of declaring "nothing changed".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import uuid

from fastapi import HTTPException, status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.bot_access import (
    add_to_audience,
    canonical_audience,
    parse_allowed_telegram_ids,
    remove_from_audience,
)
from shared.contracts.bot_rollout import (
    BOT_ROLLOUT_METADATA_KEY,
    BOT_ROLLOUT_RUN_ID_PREFIX,
    BotRolloutPublishState,
    BotRolloutRecord,
)
from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.deployment import DeploymentResult
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.queues.deploy import (
    DeployAction,
    DeployMessage,
    DeployTrigger,
)
from shared.models import Application, Deployment, Project, Run

logger = structlog.get_logger()

# The contract literal the generated bot reads, and where the deploy resolver
# expects to find it.
_BOT_ALLOWED_IDS_KEY = "TG_BOT_ALLOWED_TELEGRAM_IDS"


class AudienceOperation(StrEnum):
    """The typed mutation a caller asked for."""

    SET = "set"
    ADD = "added"
    REMOVE = "removed"


#: The legacy encrypted secret key that carried a private audience before the
#: contract endpoint existed. set_bot_access migrates it away.
LEGACY_BOT_AUDIENCE_KEY = "ADMIN_TELEGRAM_ID"


class IdempotentOutcome(StrEnum):
    """How a mutation's target state compared with the stored state."""

    CHANGED = "changed"
    ALREADY_PRESENT = "already_present"
    ALREADY_ABSENT = "already_absent"
    #: A whole-audience set whose mode + audience already match the stored ones.
    ALREADY_SET = "already_set"


@dataclass(frozen=True)
class RolloutTarget:
    """A running application and the exact commit it was deployed from."""

    application_id: int
    head_sha: str


def stored_audience(config: dict) -> str | None:
    """The project's chosen private audience, or None when there is none.

    Both a missing `bot_access` and an explicit public choice return None: a
    public bot has no list to extend, so an add refuses instead of silently
    turning the bot private, and an undecided project is told to choose first.
    """
    access = config.get("bot_access")
    if not isinstance(access, dict) or access.get("mode") == "public":
        return None
    audience = access.get("allowed_telegram_ids")
    if not isinstance(audience, str):
        return None
    return canonical_audience(audience)


async def find_live_rollout_target(db: AsyncSession, project_id: uuid.UUID) -> RolloutTarget | None:
    """The running application of *this project* and the commit it deploys.

    The join runs through `Repository` so only this project's applications can
    be selected — an application belongs to exactly one repository, and a query
    without that binding could pair this project's name with another project's
    container and SHA. Among several running applications the most recently
    *successfully deployed* one wins, matching the domain rule everywhere else
    (teardown, allocation) that reads the latest success as what actually runs.

    Returns None when nothing of this project is running, or when everything
    running lacks a recorded SHA — `find_running_without_recorded_sha` tells
    those two cases apart for the caller.
    """
    query = (
        select(Application.id, Deployment.deployed_sha)
        .join(Deployment, Deployment.application_id == Application.id)
        .where(
            Application.status == ApplicationStatus.RUNNING.value,
            Application.repo_id.in_(repository_ids_of(db, project_id)),
            Deployment.result == DeploymentResult.SUCCESS.value,
            Deployment.deployed_sha.is_not(None),
        )
        .order_by(Deployment.deployed_at.desc())
        .limit(1)
    )
    row = (await db.execute(query)).first()
    if row is None:
        return None
    return RolloutTarget(application_id=row[0], head_sha=row[1])


def repository_ids_of(db: AsyncSession, project_id: uuid.UUID):
    """The repository ids belonging to *project_id*, as an IN subquery."""
    from shared.models import Repository

    return select(Repository.id).where(Repository.project_id == project_id).scalar_subquery()


async def find_running_without_recorded_sha(db: AsyncSession, project_id: uuid.UUID) -> bool:
    """Whether a running application of this project has no successful SHA.

    Distinguished from "nothing running": something up but unidentifiable must
    refuse the rollout loudly rather than quietly skip it.
    """
    from sqlalchemy import func

    successful_shas = (
        select(func.count())
        .select_from(Deployment)
        .where(
            Deployment.application_id == Application.id,
            Deployment.result == DeploymentResult.SUCCESS.value,
            Deployment.deployed_sha.is_not(None),
        )
        .correlate(Application)
        .scalar_subquery()
    )
    query = (
        select(Application.id)
        .where(
            Application.status == ApplicationStatus.RUNNING.value,
            Application.repo_id.in_(repository_ids_of(db, project_id)),
            successful_shas == 0,
        )
        .limit(1)
    )
    return (await db.execute(query)).scalar_one_or_none() is not None


async def find_publish_owed_run(db: AsyncSession, project_id: uuid.UUID) -> Run | None:
    """This project's rollout whose queue write is still owed, if any.

    Read on the unchanged (idempotent-repeat) path: "nothing changed" must not
    hide a staged-but-unpublished rollout from an earlier interrupted attempt.
    The scheduler sweep resumes such runs regardless; surfacing it here keeps
    the API's answer honest while the sweep has not caught up yet. Oldest first,
    so a repeat names the same run until the sweep settles it.
    """
    publish_state = Run.run_metadata[(BOT_ROLLOUT_METADATA_KEY, "publish")].as_string()
    query = (
        select(Run)
        .where(
            Run.project_id == project_id,
            Run.run_metadata[BOT_ROLLOUT_METADATA_KEY].is_not(None),
            publish_state == BotRolloutPublishState.PUBLISH_OWED.value,
        )
        .order_by(Run.created_at.asc(), Run.id.asc())
        .limit(1)
    )
    return (await db.execute(query)).scalar_one_or_none()


def build_config_rollout_message(
    *,
    project_id: uuid.UUID,
    run_id: str,
    target: RolloutTarget,
    recipient_chat_id: str,
    unaddressed_reason: str,
) -> DeployMessage:
    """The config-only rollout message: same commit, same images, new env.

    The DevOps subgraph reads the project's persisted `env_overrides` when it
    rebuilds the DOTENV payload, so a plain FEATURE deploy pinned to the
    deployed SHA is what carries the audience to the running service.
    """
    return DeployMessage(
        task_id=run_id,
        project_id=str(project_id),
        telegram_chat_id=recipient_chat_id,
        unaddressed_reason=unaddressed_reason,
        story_id="",
        triggered_by=DeployTrigger.PO,
        action=DeployAction.FEATURE,
        head_sha=target.head_sha,
        env_overrides={},
    )


@dataclass(frozen=True)
class StagedRollout:
    """Everything a caller needs after the mutation transaction commits."""

    run: Run
    message: DeployMessage
    target: RolloutTarget


def stage_config_rollout(
    *,
    project: Project,
    target: RolloutTarget,
    recipient_chat_id: str,
    unaddressed_reason: str,
) -> StagedRollout:
    """Create the rollout's run row (uncommitted) and its queue message.

    The run carries the `BotRolloutRecord` in `run_metadata` — the durable
    publish intent, committed together with the audience write and *before*
    any publish. The caller owns the session and publishes only after commit;
    until then the record honestly says the publish is owed.
    """
    run_id = f"{BOT_ROLLOUT_RUN_ID_PREFIX}{uuid.uuid4().hex[:12]}"
    record = BotRolloutRecord(
        publish=BotRolloutPublishState.PUBLISH_OWED,
        application_id=target.application_id,
        head_sha=target.head_sha,
        staged_at=datetime.now(UTC),
    )
    run = Run(
        id=run_id,
        type=RunType.DEPLOY.value,
        project_id=project.id,
        status=RunStatus.QUEUED.value,
        user_id=project.owner_id,
        run_metadata={
            "application_id": target.application_id,
            "head_sha": target.head_sha,
            "triggered_by": "bot_audience_rollout",
            BOT_ROLLOUT_METADATA_KEY: record.model_dump(mode="json"),
        },
    )
    message = build_config_rollout_message(
        project_id=project.id,
        run_id=run_id,
        target=target,
        recipient_chat_id=recipient_chat_id,
        unaddressed_reason=unaddressed_reason,
    )
    return StagedRollout(run=run, message=message, target=target)


def apply_audience_mutation(config: dict, *, updated: str) -> dict:
    """Write the mutated audience into both places the contract keeps it.

    Returns a fresh config dict; the caller assigns it onto the locked project.
    Unrelated keys — secrets, tree, agent_type, unrelated overrides — are
    carried over untouched because only these two entries are rewritten.
    """
    new_config = dict(config)
    overrides = dict(new_config.get("env_overrides") or {})
    overrides[_BOT_ALLOWED_IDS_KEY] = updated
    access = dict(new_config["bot_access"])
    access["allowed_telegram_ids"] = updated
    new_config["env_overrides"] = overrides
    new_config["bot_access"] = access
    return new_config


def resolve_updated_audience(
    stored: str,
    telegram_id: int,
    operation: AudienceOperation,
) -> str:
    """Compute the audience after one typed one-ID mutation.

    Removal that would empty a private audience raises 422: going public is
    set_bot_access's explicit decision, never a removal side effect. SET never
    passes through here — it rewrites the whole contract location itself.
    """
    match operation:
        case AudienceOperation.ADD:
            return add_to_audience(stored, telegram_id)
        case AudienceOperation.REMOVE:
            updated = remove_from_audience(stored, telegram_id)
            if not parse_allowed_telegram_ids(updated):
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"Telegram ID {telegram_id} is the final allowed ID — removing "
                        "it would make the bot public. Use /config/bot-access with "
                        "mode='public' if that is what you want."
                    ),
                )
            return updated
    raise ValueError(f"unknown audience operation: {operation}")


def no_private_audience_detail(config: dict) -> str:
    """Why an add/remove cannot proceed: public by choice, or never chosen."""
    access = config.get("bot_access")
    if isinstance(access, dict) and access.get("mode") == "public":
        return (
            "this project has no private bot audience to change — choose one with "
            "/config/bot-access first"
        )
    return (
        "no bot audience has been chosen for this project — set one with "
        "/config/bot-access before adding or removing users"
    )


def unrecorded_target_detail() -> str:
    """Why a rollout was refused: running but not attributable to a commit."""
    return (
        "the bot is running but its deployed commit is not recorded — a "
        "configuration-only rollout cannot be started safely; redeploy the "
        "project first"
    )


__all__ = [
    "AudienceOperation",
    "IdempotentOutcome",
    "RolloutTarget",
    "StagedRollout",
    "apply_audience_mutation",
    "build_config_rollout_message",
    "find_live_rollout_target",
    "find_publish_owed_run",
    "find_running_without_recorded_sha",
    "no_private_audience_detail",
    "resolve_updated_audience",
    "stage_config_rollout",
    "stored_audience",
    "unrecorded_target_detail",
]
