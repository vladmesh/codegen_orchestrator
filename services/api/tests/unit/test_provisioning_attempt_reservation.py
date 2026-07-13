from unittest.mock import AsyncMock, MagicMock

from shared.contracts.dto.server import ProvisioningAttemptReservation
from src.routers.servers import reserve_provisioning_attempt


async def test_reservation_uses_conditional_atomic_increment():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 1
    db.execute.return_value = result

    response = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )

    statement = db.execute.await_args.args[0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "provisioning_attempts < 3" in compiled
    assert "provisioning_attempts=(servers.provisioning_attempts + 1)" in compiled
    assert response.reserved is True
    assert response.provisioning_attempts == 1
    db.commit.assert_awaited_once()


async def test_reservation_at_limit_returns_persisted_count_without_increment():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result
    db.get.return_value = MagicMock(provisioning_attempts=3)

    response = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )

    assert response.reserved is False
    assert response.provisioning_attempts == 3
    db.commit.assert_not_awaited()
