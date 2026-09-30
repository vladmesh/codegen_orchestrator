"""A project's verification gaps: checks its settled QA runs could not perform."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.qa_verification import (
    QAVerificationGap,
    QAVerificationGapsFromRun,
    QAVerificationGapsRecorded,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QARunResult
from shared.contracts.queues.qa import QAOutcome
from shared.models import Project, Run, VerificationGap

from ...database import get_async_session
from ...dependencies import (
    _optional_bearer_scheme,
    is_internal_service,
    require_internal_or_admin,
    resolve_actor,
)

logger = structlog.get_logger()
router = APIRouter()

#: The QA outcomes that are a verdict on the product. A blocked or errored run
#: judged nothing, so what it did not check is not a gap of the product.
_VERDICT_OUTCOMES = frozenset({QAOutcome.PASSED, QAOutcome.FAILED, QAOutcome.EXHAUSTED})


@router.get("/{project_id}/verification-gaps", response_model=list[QAVerificationGap])
async def list_verification_gaps(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_session),
    _internal_or_admin: None = Depends(require_internal_or_admin),
) -> list[VerificationGap]:
    """Every check this project's settled QA runs could not perform, oldest first."""
    if await db.get(Project, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    rows = await db.execute(
        select(VerificationGap)
        .where(VerificationGap.project_id == project_id)
        .order_by(VerificationGap.created_at, VerificationGap.id)
    )
    return list(rows.scalars().all())


@router.post("/{project_id}/verification-gaps/from-run", response_model=QAVerificationGapsRecorded)
async def record_verification_gaps_from_run(  # noqa: PLR0913 — FastAPI dependencies, each one named
    project_id: uuid.UUID,
    body: QAVerificationGapsFromRun,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> QAVerificationGapsRecorded:
    """Write a settled QA Run's unverified checks on its project, once per check.

    The gaps are read off the Run's own stored result, not the request, and
    only from a Run settled with a verdict on the product. Writing the same Run
    again adds nothing: a check already recorded for this run is counted, not
    repeated.
    """
    actor = await resolve_actor(
        is_internal=_is_internal, telegram_id=x_telegram_id, credentials=credentials, db=db
    )
    if actor is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the QA runtime records verification gaps",
        )
    # The project row serializes concurrent writes of the same run.
    project = (
        await db.execute(select(Project).where(Project.id == project_id).with_for_update())
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    run = await db.get(Run, body.run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.project_id != project_id or run.type != RunType.QA.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run {body.run_id} is not a QA run of project {project_id}",
        )
    try:
        result = QARunResult.model_validate(run.result or {})
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run {body.run_id} carries no QA result: {exc.error_count()} error(s)",
        ) from exc
    if run.status != RunStatus.COMPLETED.value or result.qa_outcome not in _VERDICT_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run {body.run_id} did not settle with a verdict; it records no gaps",
        )

    existing = {
        row.name
        for row in (
            await db.execute(
                select(VerificationGap).where(
                    VerificationGap.project_id == project_id,
                    VerificationGap.run_id == body.run_id,
                )
            )
        ).scalars()
    }
    recorded: list[str] = []
    already = 0
    for check in result.unverified_checks:
        if check.name in existing:
            already += 1
            continue
        db.add(
            VerificationGap(
                project_id=project_id,
                story_id=run.story_id,
                run_id=body.run_id,
                name=check.name,
                reason=check.reason,
                origin=check.origin.value,
            )
        )
        existing.add(check.name)
        recorded.append(check.name)
    await db.commit()
    logger.info(
        "verification_gaps_recorded",
        project_id=str(project_id),
        run_id=body.run_id,
        recorded=recorded,
        already_recorded=already,
    )
    return QAVerificationGapsRecorded(recorded=recorded, already_recorded=already)
