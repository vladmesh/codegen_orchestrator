"""A project's QA probe library: read by admins and the QA runtime, filled from passed runs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.qa_probe_library import (
    QA_PROBE_LIBRARY_CAP,
    QAProbeLibraryEntry,
    QAProbeLibraryStored,
    QAProbeLibraryStoreFromRun,
    is_probe_library_name,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import QAProbeRun, QARunResult
from shared.contracts.queues.qa import QAOutcome
from shared.models import Project, QAProbe, Run

from ...database import get_async_session
from ...dependencies import (
    _optional_bearer_scheme,
    is_internal_service,
    require_internal_or_admin,
    resolve_actor,
)

logger = structlog.get_logger()
router = APIRouter()


@router.get("/{project_id}/qa-probes", response_model=list[QAProbeLibraryEntry])
async def list_qa_probes(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_async_session),
    _internal_or_admin: None = Depends(require_internal_or_admin),
) -> list[QAProbe]:
    """Every stored probe of this project's library, by platform and name."""
    if await db.get(Project, project_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    rows = await db.execute(
        select(QAProbe)
        .where(QAProbe.project_id == project_id)
        .order_by(QAProbe.platform, QAProbe.name)
    )
    return list(rows.scalars().all())


def _library_candidates(result: QARunResult) -> tuple[dict[tuple[str, str], QAProbeRun], int]:
    """The probes a passed run contributes, and how many it named out of the library's reach.

    A probe contributes when it exited 0 with its whole source and a known file
    kind. Its name is the executor's choice, so this is where the library
    decides which names exist: one that is not a library name is skipped and
    counted, never rewritten, and every later run can lay the library out by
    name. A name the run used twice keeps its last record.
    """
    candidates: dict[tuple[str, str], QAProbeRun] = {}
    skipped = 0
    for probe in result.probe_runs or []:
        if probe.exit_status != 0 or probe.source_truncated or probe.file_kind is None:
            continue
        if not is_probe_library_name(probe.name):
            skipped += 1
            continue
        candidates[(probe.platform.value, probe.name)] = probe
    return candidates, skipped


def _over_cap(rows: Iterable[QAProbe]) -> list[QAProbe]:
    """The entries past the project cap: the oldest `updated_at` go first.

    Rows one store wrote share its timestamp; the later-inserted id is the newer.
    """
    ranked = sorted(rows, key=lambda row: (row.updated_at, row.id or 0), reverse=True)
    return ranked[QA_PROBE_LIBRARY_CAP:]


@router.post("/{project_id}/qa-probes/from-run", response_model=QAProbeLibraryStored)
async def store_qa_probes_from_run(  # noqa: PLR0913 — FastAPI dependencies, each one named
    project_id: uuid.UUID,
    body: QAProbeLibraryStoreFromRun,
    db: AsyncSession = Depends(get_async_session),
    x_telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    _is_internal: bool = Depends(is_internal_service),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> QAProbeLibraryStored:
    """Upsert a passed QA Run's probes into its project's library, then enforce the cap.

    The entries are read off the Run's own stored result, not the request, so
    only probes the capability endpoint already scrubbed and bounded can enter
    the library, and only from a Run that is settled as passed.
    """
    actor = await resolve_actor(
        is_internal=_is_internal, telegram_id=x_telegram_id, credentials=credentials, db=db
    )
    if actor is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only the QA runtime stores probes in a library",
        )
    # The project row serializes concurrent stores, so eviction counts one state.
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
    if run.status != RunStatus.COMPLETED.value or result.qa_outcome != QAOutcome.PASSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run {body.run_id} is not a passed QA run; only a pass stores probes",
        )

    existing = {
        (row.platform, row.name): row
        for row in (
            await db.execute(select(QAProbe).where(QAProbe.project_id == project_id))
        ).scalars()
    }
    now = datetime.now(UTC)
    stored: list[str] = []
    candidates, skipped = _library_candidates(result)
    for key, probe in candidates.items():
        row = existing.get(key)
        if row is None:
            row = QAProbe(project_id=project_id, platform=key[0], name=key[1], created_at=now)
            db.add(row)
            existing[key] = row
        row.source = probe.source
        row.file_kind = probe.file_kind.value
        row.origin_run_id = body.run_id
        row.updated_at = now
        stored.append(f"{key[0]}/{key[1]}")
    await db.flush()

    evicted: list[str] = []
    for row in _over_cap(existing.values()):
        evicted.append(f"{row.platform}/{row.name}")
        await db.delete(row)
    await db.commit()
    logger.info(
        "qa_probe_library_stored",
        project_id=str(project_id),
        run_id=body.run_id,
        stored=stored,
        evicted=evicted,
        skipped=skipped,
    )
    return QAProbeLibraryStored(stored=stored, evicted=evicted, skipped=skipped)
