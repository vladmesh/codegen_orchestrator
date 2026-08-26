"""Count-admission decisions are typed, auditable, and held under row locks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.dto.run import RunType
from shared.contracts.dto.work_admission import (
    PaidRunStartCommand,
    WorkAdmissionOutcome,
    WorkAdmissionReason,
)
from src.work_admission import admit_project_creation, start_paid_run


def _rows(values: dict[str, object]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        SimpleNamespace(key=key, value=value) for key, value in values.items()
    ]
    return result


@pytest.mark.asyncio
async def test_project_stop_is_checked_before_the_project_count():
    db = AsyncMock()
    db.add = MagicMock()
    db.scalars.return_value = _rows({"work_admission.emergency_stop": True})

    admission = await admit_project_creation(7, False, db)

    assert admission.outcome is WorkAdmissionOutcome.DENIED
    assert admission.reason is WorkAdmissionReason.EMERGENCY_STOP
    assert db.scalar.await_count == 1


@pytest.mark.asyncio
async def test_paid_run_start_adds_the_queued_run_before_returning_admitted():
    db = AsyncMock()
    db.add = MagicMock()
    db.scalars.side_effect = [
        _rows({}),
        _rows({"work_admission.emergency_stop": False}),
        _rows({}),
        _rows({"work_admission.max_concurrent_paid_runs": 1}),
    ]
    db.scalar.side_effect = [None, SimpleNamespace(owner_id=7), None, None, 0]

    result = await start_paid_run(
        PaidRunStartCommand(
            id="qa-1",
            type=RunType.QA,
            project_id="00000000-0000-0000-0000-000000000001",
        ),
        db,
    )

    assert result.admission.outcome is WorkAdmissionOutcome.ADMITTED
    assert result.run_id == "qa-1"
    assert db.add.call_count == 2  # Run plus the durable admission audit record.
