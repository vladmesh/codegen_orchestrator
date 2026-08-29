"""Stories router — CRUD + action-based status transitions."""

from datetime import UTC, datetime
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.owner_notification import (
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QABlocker, QABlockerCategory
from shared.contracts.dto.story import (
    VALID_TRANSITIONS,
    StoryRecheck,
    StoryRecheckMode,
    StoryStatus,
)
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.deploy import DeployAction, DeployMessage, DeployTrigger
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.vocab import OwnerNotificationEvent
from shared.models.application import Application
from shared.models.repository import Repository
from shared.models.run import Run
from shared.models.story import Story
from shared.queues import ARCHITECT_QUEUE, DEPLOY_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_accept_result_actor, get_redis_client, require_internal_or_admin
from ..schemas.actions import AdminAction
from ..schemas.story import (
    StoryAccept,
    StoryAcceptance,
    StoryCreate,
    StoryOwnerNotificationRead,
    StoryRead,
    StoryReopen,
    StoryTransition,
    StoryUpdate,
)
from ._recipients import resolve_project_chat_id, resolve_project_recipient
from .applications import _make_deploy_run_id

logger = structlog.get_logger()

router = APIRouter(prefix="/stories", tags=["stories"])

_DEFAULT_COMPLETION_NOTIFICATION_TEXT = (
    "The story is finished. Tell the user the good news that their product is ready."
)

_DEFAULT_ACCEPTED_COMPLETION_NOTIFICATION_TEXT = (
    "The story is finished: an operator accepted the result. Tell the user the good news "
    "that their product is ready."
)

OWNER_NOTIFICATION_PAGE_MAX = 500

_RECHECKABLE_BLOCKERS = frozenset(
    {
        QABlockerCategory.QA_EXECUTOR_UNAVAILABLE,
        QABlockerCategory.DEPLOYED_URL_UNREACHABLE,
        QABlockerCategory.QA_PROBE_UNAVAILABLE,
        QABlockerCategory.TELEGRAM_PROBE_UNDELIVERED,
        QABlockerCategory.SERVER_UNAVAILABLE,
        QABlockerCategory.QA_ACCESS_GRANT_FAILED,
        QABlockerCategory.QA_ACCESS_EXPIRED,
    }
)
_COMMIT_SHA_LENGTHS = frozenset({40, 64})


def _generate_id() -> str:
    return f"story-{secrets.token_hex(4)}"


async def _get_story(story_id: str, db: AsyncSession) -> Story:
    query = select(Story).where(Story.id == story_id)
    result = await db.execute(query)
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Story {story_id} not found",
        )
    return story


def _validate_transition(from_status: str, to_status: str) -> None:
    try:
        from_s = StoryStatus(from_status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {from_status}",
        ) from e
    try:
        to_s = StoryStatus(to_status)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid status: {to_status}",
        ) from e
    if to_s not in VALID_TRANSITIONS[from_s]:
        allowed = [s.value for s in VALID_TRANSITIONS[from_s]]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot transition from {from_status} to {to_status}. Allowed: {allowed}",
        )


# --- CRUD ---


async def _validate_retry_parent(body: StoryCreate, db: AsyncSession) -> None:
    if body.parent_story_id is None:
        return

    query = (
        select(Story)
        .where(
            Story.id == body.parent_story_id,
            Story.project_id == body.project_id,
        )
        .with_for_update()
    )
    parent = (await db.execute(query)).scalar_one_or_none()
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parent story {body.parent_story_id} not found for project",
        )

    reason = parent.quarantine_reason or {}
    if parent.status == StoryStatus.WAITING_HUMAN_REVIEW.value and isinstance(
        reason.get("qa_failure"), dict
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Story {parent.id} requires human review before retrying",
        )


@router.post("/", response_model=StoryRead, status_code=status.HTTP_201_CREATED)
async def create_story(
    body: StoryCreate,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    await _validate_retry_parent(body, db)
    now = datetime.now(UTC)
    story = Story(
        id=_generate_id(),
        project_id=body.project_id,
        parent_story_id=body.parent_story_id,
        title=body.title,
        description=body.description,
        acceptance_criteria=body.acceptance_criteria,
        type=body.type,
        status=StoryStatus.CREATED.value,
        priority=body.priority,
        blocked_by_story_id=body.blocked_by_story_id,
        created_by=body.created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(story)
    await db.commit()
    await db.refresh(story)

    logger.info("story_created", story_id=story.id, title=story.title)
    return StoryRead.model_validate(story, from_attributes=True)


@router.get("/", response_model=list[StoryRead])
async def list_stories(
    project_id: uuid.UUID | None = None,
    status_filter: str | None = Query(None, alias="status"),
    parent_story_id: str | None = Query(None),
    type_filter: str | None = Query(None, alias="type"),
    priority: int | None = Query(None),
    sort: str | None = Query(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[StoryRead]:
    query = select(Story)

    if project_id:
        query = query.where(Story.project_id == project_id)
    if status_filter:
        query = query.where(Story.status == status_filter)
    if parent_story_id:
        query = query.where(Story.parent_story_id == parent_story_id)
    if type_filter:
        query = query.where(Story.type == type_filter)
    if priority is not None:
        query = query.where(Story.priority == priority)

    if sort == "-created_at":
        query = query.order_by(Story.created_at.desc())
    elif sort == "created_at":
        query = query.order_by(Story.created_at.asc())
    elif sort == "-priority":
        query = query.order_by(Story.priority.desc(), Story.created_at.asc())
    else:
        query = query.order_by(Story.priority.asc(), Story.created_at.asc())

    result = await db.execute(query)
    items = result.scalars().all()
    return [StoryRead.model_validate(s, from_attributes=True) for s in items]


@router.get("/owner-notifications/owed", response_model=list[StoryOwnerNotificationRead])
async def list_stories_owing_owner_notification(
    limit: int = Query(100, ge=1, le=OWNER_NOTIFICATION_PAGE_MAX),
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> list[StoryOwnerNotificationRead]:
    """Completed stories whose durable completion message is still owed."""
    notification = Story.owner_notification
    state = Story.owner_notification[("state")].as_string()
    query = (
        select(Story)
        .where(notification.is_not(None), state == OwnerNotificationState.OWED.value)
        .order_by(Story.created_at.asc(), Story.id.asc())
        .limit(limit)
    )
    result = await db.execute(query)
    return [
        StoryOwnerNotificationRead(
            id=story.id,
            owner_notification=OwnerNotification.model_validate(story.owner_notification),
        )
        for story in result.scalars().all()
    ]


@router.get("/{story_id}/owner-notification", response_model=OwnerNotification)
async def get_story_owner_notification(
    story_id: str,
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> OwnerNotification:
    """Read the completion record the completion transaction created."""
    story = await _get_story(story_id, db)
    if story.owner_notification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Owner notification not found"
        )
    return OwnerNotification.model_validate(story.owner_notification)


@router.patch("/{story_id}/owner-notification", response_model=OwnerNotification)
async def update_story_owner_notification(
    story_id: str,
    notification: OwnerNotification,
    db: AsyncSession = Depends(get_async_session),
    _is_internal: bool = Depends(require_internal_or_admin),
) -> OwnerNotification:
    """Settle the story-backed completion notification after one delivery attempt."""
    story = await _get_story(story_id, db)
    story.owner_notification = notification.model_dump(mode="json")
    await db.commit()
    return notification


@router.get("/{story_id}", response_model=StoryRead)
async def get_story(
    story_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    story = await _get_story(story_id, db)
    return StoryRead.model_validate(story, from_attributes=True)


@router.patch("/{story_id}", response_model=StoryRead)
async def update_story(
    story_id: str,
    body: StoryUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    story = await _get_story(story_id, db)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(story, field, value)

    await db.commit()
    await db.refresh(story)

    logger.info("story_updated", story_id=story.id, fields=list(update_data.keys()))
    return StoryRead.model_validate(story, from_attributes=True)


# --- Action endpoints (state machine transitions) ---


def _do_transition(story: Story, to_status: StoryStatus) -> None:
    _validate_transition(story.status, to_status.value)
    story.status = to_status.value


async def _completion_notification_text(
    story: Story,
    db: AsyncSession,
    *,
    acceptance: StoryAcceptance | None = None,
) -> str:
    """Keep a live current-cycle deployment address, never stale QA evidence."""
    fallback = (
        _DEFAULT_ACCEPTED_COMPLETION_NOTIFICATION_TEXT
        if acceptance is not None
        else _DEFAULT_COMPLETION_NOTIFICATION_TEXT
    )
    query = (
        select(Run)
        .where(
            Run.story_id == story.id,
            Run.type == RunType.QA.value,
            Run.status == RunStatus.COMPLETED.value,
        )
        .order_by(Run.completed_at.desc(), Run.id.desc())
        .limit(1)
    )
    if story.reopened_at is not None:
        query = query.where(Run.created_at >= story.reopened_at)
    run = (await db.execute(query)).scalars().first()
    if run is None or not isinstance(run.result, dict):
        return fallback

    outcome = run.result.get("qa_outcome")
    if outcome != QAOutcome.PASSED and acceptance is None:
        return fallback
    if QA_HANDOFF_KEY not in run.run_metadata:
        if outcome == QAOutcome.PASSED:
            raise KeyError(QA_HANDOFF_KEY)
        return fallback

    qa_message = QAHandoffPlan.model_validate(run.run_metadata[QA_HANDOFF_KEY]).qa_message
    application_status = (
        await db.execute(
            select(Application.status).where(Application.id == qa_message.application_id)
        )
    ).scalar_one_or_none()
    # QA's passed verdict is itself live address evidence. An acceptance has no
    # such verdict, so it retains the running gate and never tells an owner a
    # stopped quarantine target is reachable.
    if acceptance is not None and application_status != ApplicationStatus.RUNNING.value:
        return fallback

    address = qa_message.deployed_url
    if qa_message.bot_username:
        address = f"{address} (Telegram bot @{qa_message.bot_username})"
    if acceptance is not None:
        return (
            "The story is finished: an operator accepted the deployed result. "
            f"Tell the user the good news and give them the address: {address}"
        )
    return (
        "The story is finished: it is deployed and QA passed. Tell the user the good "
        f"news and give them the address: {address}"
    )


async def _owe_completed_story_notification(
    story: Story,
    db: AsyncSession,
    *,
    acceptance: StoryAcceptance | None = None,
) -> None:
    """Attach the completion obligation to the story in its transition transaction."""
    story.owner_notification = OwnerNotification(
        event=OwnerNotificationEvent.STORY_COMPLETED,
        text=await _completion_notification_text(story, db, acceptance=acceptance),
        story_id=story.id,
        project_id=str(story.project_id),
        terminal_status=StoryStatus.COMPLETED,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    ).model_dump(mode="json")


async def _complete_story(
    story: Story,
    db: AsyncSession,
    *,
    acceptance: StoryAcceptance | None = None,
) -> StoryRead:
    """The one completion transaction used by ordinary and accepted-result routes."""
    await _owe_completed_story_notification(story, db, acceptance=acceptance)
    _do_transition(story, StoryStatus.COMPLETED)
    if acceptance is not None:
        story.operator_acceptance = acceptance.model_dump(mode="json")
        # The completed story no longer represents a live QA quarantine.
        story.quarantine_reason = None
    elif story.quarantine_reason is not None:
        latest_qa = (
            (
                await db.execute(
                    select(Run)
                    .where(
                        Run.story_id == story.id,
                        Run.type == RunType.QA.value,
                        Run.status == RunStatus.COMPLETED.value,
                    )
                    .order_by(Run.completed_at.desc(), Run.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if (
            latest_qa is not None
            and isinstance(latest_qa.result, dict)
            and latest_qa.result.get("qa_outcome") == QAOutcome.PASSED
        ):
            # Only the ordinary green QA verdict retires a recheck quarantine.
            story.quarantine_reason = None
    await db.commit()
    await db.refresh(story)
    return StoryRead.model_validate(story, from_attributes=True)


async def _recheck_target(
    story: Story, db: AsyncSession
) -> tuple[Application, Repository, str, str]:
    """Return the exact QA target which produced the typed quarantine.

    The newest story-linked QA Run is the capability receipt. It binds the
    blocker to one application instead of allowing an implied target selection.
    """
    reason = story.quarantine_reason
    if not isinstance(reason, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa requires a typed QA quarantine reason",
        )
    try:
        blocker = QABlocker.model_validate(reason["blocker"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa requires a typed QA blocker",
        ) from exc
    if blocker.category not in _RECHECKABLE_BLOCKERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"QA blocker '{blocker.category.value}' cannot be cleared by recheck",
        )

    receipt = (
        (
            await db.execute(
                select(Run)
                .where(Run.story_id == story.id, Run.type == RunType.QA.value)
                .order_by(Run.completed_at.desc(), Run.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    application_id = (receipt.run_metadata or {}).get("application_id") if receipt else None
    if not isinstance(application_id, int):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa cannot find an application in the story QA capability receipt",
        )
    application = await db.get(Application, application_id)
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa target application no longer exists",
        )
    repository = await db.get(Repository, application.repo_id)
    if repository is None or repository.project_id != story.project_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa target no longer matches the story capability receipt",
        )
    deploy_receipt = (
        (
            await db.execute(
                select(Run)
                .where(
                    Run.story_id == story.id,
                    Run.type == RunType.DEPLOY.value,
                    Run.created_at <= receipt.created_at,
                )
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    head_sha = (deploy_receipt.run_metadata or {}).get("head_sha") if deploy_receipt else None
    if not isinstance(head_sha, str) or len(head_sha) not in _COMMIT_SHA_LENGTHS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa target has no deployed SHA in its capability receipt",
        )
    return application, repository, deploy_receipt.id, head_sha


@router.post("/{story_id}/human-review", response_model=StoryRead)
async def human_review_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Move a blocked active story to the visible human-review queue."""
    body = body or StoryTransition()
    story = await _get_story(story_id, db)
    _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
    await db.commit()
    await db.refresh(story)
    logger.info("story_waiting_human_review", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/wait-user-secret", response_model=StoryRead)
async def wait_user_secret_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Park a deploying story that needs a user secret it does not have yet.

    The scheduler re-dispatches the deploy (→ DEPLOYING) once the secret appears,
    so this is a non-terminal wait, not a failure.
    """
    body = body or StoryTransition()
    story = await _get_story(story_id, db)
    _do_transition(story, StoryStatus.WAITING_USER_SECRET)
    await db.commit()
    await db.refresh(story)
    logger.info("story_waiting_user_secret", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/start", response_model=StoryRead)
async def start_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    if story.blocked_by_story_id:
        blocker = await _get_story(story.blocked_by_story_id, db)
        if blocker.status != StoryStatus.COMPLETED.value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Cannot start: blocked by story {blocker.id} "
                    f"(status: {blocker.status}, must be completed)"
                ),
            )

    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    logger.info("story_started", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/complete", response_model=StoryRead)
async def complete_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)
    if story.status == StoryStatus.WAITING_HUMAN_REVIEW.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Use accept-result to complete a story in waiting_human_review",
        )

    logger.info("story_completed", story_id=story.id, actor=body.actor)
    return await _complete_story(story, db)


@router.post("/{story_id}/accept-result", response_model=StoryRead)
async def accept_story_result(
    story_id: str,
    body: StoryAccept,
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_accept_result_actor),
) -> StoryRead:
    """Let the authenticated administrator finish a reviewed result with evidence."""
    story = await _get_story(story_id, db)
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="accept-result requires a story in waiting_human_review",
        )
    acceptance = StoryAcceptance(
        actor=actor,
        basis=body.basis,
        accepted_at=datetime.now(UTC),
        overridden_quarantine_reason=story.quarantine_reason,
    )
    completed = await _complete_story(story, db, acceptance=acceptance)
    logger.info(
        "story_result_accepted",
        story_id=story.id,
        actor=acceptance.actor,
        basis=acceptance.basis,
    )
    return completed


@router.post("/{story_id}/recheck-qa", response_model=StoryRead)
async def recheck_story_qa(
    story_id: str,
    body: StoryAccept,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    actor: str = Depends(get_accept_result_actor),
) -> StoryRead:
    """Re-enter a typed QA quarantine through the ordinary deploy or QA route."""
    story = (
        await db.execute(select(Story).where(Story.id == story_id).with_for_update())
    ).scalar_one_or_none()
    if story is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Story {story_id} not found"
        )
    if story.operator_recheck is not None:
        return StoryRead.model_validate(story, from_attributes=True)
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="recheck-qa requires a story in waiting_human_review",
        )

    application, _repository, source_deploy_run_id, head_sha = await _recheck_target(story, db)
    recheck_id = f"recheck-{uuid.uuid4().hex}"
    snapshot = dict(story.quarantine_reason or {})

    if application.status not in {ApplicationStatus.STOPPED.value, ApplicationStatus.RUNNING.value}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"recheck-qa target application is '{application.status}', not stopped or running"
            ),
        )

    run_id = _make_deploy_run_id()
    recipient = await resolve_project_recipient(
        db, story.project_id, event="qa_recheck_deploy", story_id=story.id
    )
    message = DeployMessage(
        task_id=run_id,
        project_id=str(story.project_id),
        telegram_chat_id=recipient.telegram_chat_id,
        unaddressed_reason=recipient.unaddressed_reason,
        story_id=story.id,
        triggered_by=DeployTrigger.ADMIN,
        action=DeployAction.CREATE,
        head_sha=head_sha,
        application_id=application.id,
    )
    db.add(
        Run(
            id=run_id,
            type=RunType.DEPLOY.value,
            project_id=story.project_id,
            story_id=story.id,
            run_metadata={
                "application_id": application.id,
                "recheck_id": recheck_id,
                "head_sha": head_sha,
                "source_deploy_run_id": source_deploy_run_id,
                "recheck_message": message.model_dump(mode="json"),
            },
        )
    )
    application.status = ApplicationStatus.DEPLOYING.value
    mode = StoryRecheckMode.DEPLOY

    audit = StoryRecheck(
        id=recheck_id,
        actor=actor,
        basis=body.basis,
        rechecked_at=datetime.now(UTC),
        mode=mode,
        application_id=application.id,
        run_id=run_id,
        rechecked_quarantine_reason=snapshot,
    )
    story.operator_recheck = audit.model_dump(mode="json")
    _do_transition(story, StoryStatus.DEPLOYING)
    await db.commit()
    await db.refresh(story)

    try:
        await redis.publish_message(DEPLOY_QUEUE, message)
    except Exception as exc:
        logger.exception(
            "story_qa_recheck_publish_outcome_unknown", story_id=story.id, run_id=run_id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="QA recheck handoff could not be confirmed",
        ) from exc

    logger.info(
        "story_qa_recheck_requested",
        story_id=story.id,
        actor=actor,
        mode=mode.value,
        run_id=run_id,
        application_id=application.id,
    )
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/fail", response_model=StoryRead)
async def fail_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.FAILED)
    await db.commit()
    await db.refresh(story)

    logger.info("story_failed", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/pr_review", response_model=StoryRead)
async def pr_review_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.PR_REVIEW)
    await db.commit()
    await db.refresh(story)

    logger.info("story_pr_review", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/deploy", response_model=StoryRead)
async def deploy_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.DEPLOYING)
    await db.commit()
    await db.refresh(story)

    logger.info("story_deploying", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/test", response_model=StoryRead)
async def test_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.TESTING)
    await db.commit()
    await db.refresh(story)

    logger.info("story_testing", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/reopen", response_model=StoryRead)
async def reopen_story(
    story_id: str,
    body: StoryReopen | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryReopen()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.REOPENED)
    story.reopened_at = datetime.now(UTC)
    if body.user_report is not None:
        story.user_report = body.user_report

    await db.commit()
    await db.refresh(story)

    logger.info("story_reopened", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/archive", response_model=StoryRead)
async def archive_story(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    body = body or StoryTransition()
    story = await _get_story(story_id, db)

    _do_transition(story, StoryStatus.ARCHIVED)
    await db.commit()
    await db.refresh(story)

    logger.info("story_archived", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)


@router.post("/{story_id}/send-to-architect", response_model=StoryRead)
async def send_to_architect(
    story_id: str,
    body: AdminAction | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
) -> StoryRead:
    """Send a story to the architect for decomposition.

    Validates status is CREATED or REOPENED, transitions to IN_PROGRESS,
    and publishes ArchitectMessage to architect:queue.
    """
    body = body or AdminAction()
    story = await _get_story(story_id, db)

    allowed = {StoryStatus.CREATED.value, StoryStatus.REOPENED.value}
    if story.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Cannot send to architect from status '{story.status}'."
                " Must be created or reopened."
            ),
        )

    is_reopen = story.status == StoryStatus.REOPENED.value

    # Transition: CREATED/REOPENED → IN_PROGRESS
    _do_transition(story, StoryStatus.IN_PROGRESS)
    await db.commit()
    await db.refresh(story)

    # Publish to architect queue
    msg = ArchitectMessage(
        story_id=story.id,
        project_id=str(story.project_id),
        telegram_chat_id=await resolve_project_chat_id(
            db, story.project_id, event="story_sent_to_architect", story_id=story.id
        ),
        is_reopen=is_reopen,
        user_report=story.user_report if is_reopen else None,
    )
    await redis.publish_message(ARCHITECT_QUEUE, msg)

    logger.info("story_sent_to_architect", story_id=story.id, actor=body.actor, is_reopen=is_reopen)
    return StoryRead.model_validate(story, from_attributes=True)
