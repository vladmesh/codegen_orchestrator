from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from shared.contracts.dto.incident import IncidentCreate, IncidentType
from src.routers.incidents import record_provisioning_failure


async def test_record_provisioning_failure_is_atomic_upsert():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = MagicMock(id=7, recovery_attempts=2)
    db.execute.return_value = result

    incident = await record_provisioning_failure(
        IncidentCreate(
            server_handle="srv-1",
            incident_type=IncidentType.PROVISIONING_FAILED,
            details={"step": "software_setup"},
        ),
        db,
        None,
    )

    statement = db.execute.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in compiled
    assert "recovery_attempts = (incidents.recovery_attempts +" in compiled
    assert "status IN ('detected', 'recovering')" in compiled
    assert incident.id == 7
    db.commit.assert_awaited_once()
