"""Count-admission decisions are typed, auditable, and held under row locks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.contracts.dto.work_admission import WorkAdmissionOutcome, WorkAdmissionReason
from src.work_admission import admit_paid_work, admit_project_creation


def _rows(values: dict[str, object]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = [
        SimpleNamespace(key=key, value=value) for key, value in values.items()
    ]
    return result


@pytest.mark.asyncio
async def test_paid_work_limit_defers_and_writes_a_typed_audit_record():
    db = AsyncMock()
    db.add = MagicMock()
    db.scalars.return_value = _rows(
        {
            "work_admission.emergency_stop": False,
            "work_admission.max_concurrent_paid_runs": 2,
        }
    )
    db.scalar.return_value = 2

    admission = await admit_paid_work("eng-1", db)

    assert admission.outcome is WorkAdmissionOutcome.DEFERRED
    assert admission.reason is WorkAdmissionReason.PAID_WORK_LIMIT
    assert admission.retryable is True
    audit = db.add.call_args.args[0]
    assert audit.subject == "paid_work"
    assert audit.reason == "paid_work_limit"


@pytest.mark.asyncio
async def test_project_stop_is_checked_before_the_project_count():
    db = AsyncMock()
    db.add = MagicMock()
    db.scalars.return_value = _rows({"work_admission.emergency_stop": True})

    admission = await admit_project_creation(7, False, db)

    assert admission.outcome is WorkAdmissionOutcome.DENIED
    assert admission.reason is WorkAdmissionReason.EMERGENCY_STOP
    assert db.scalar.await_count == 1
