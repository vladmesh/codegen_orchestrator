from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from shared.contracts.dto.server import (
    ProvisioningAttemptReservation,
    ProvisioningAttemptReset,
)
from src.routers.servers import reserve_provisioning_attempt, reset_provisioning_attempts


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


class InMemoryAttemptSession:
    def __init__(self, attempts: int):
        self.server = SimpleNamespace(provisioning_attempts=attempts)
        self.commits = 0

    async def execute(self, statement):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "provisioning_attempts <" in compiled:
            max_attempts = int(compiled.rsplit("<", maxsplit=1)[1].split()[0])
            value = (
                self.server.provisioning_attempts + 1
                if self.server.provisioning_attempts < max_attempts
                else None
            )
            if value is not None:
                self.server.provisioning_attempts = value
        else:
            expected_attempt = int(compiled.rsplit("=", maxsplit=1)[1].split()[0])
            value = 0 if self.server.provisioning_attempts == expected_attempt else None
            if value is not None:
                self.server.provisioning_attempts = value
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        return result

    async def get(self, _model, _handle):
        return self.server

    async def commit(self):
        self.commits += 1


async def test_successful_episode_resets_persisted_attempts_and_next_reservation_starts_at_one():
    db = InMemoryAttemptSession(attempts=2)

    reset = await reset_provisioning_attempts(
        "srv-1", ProvisioningAttemptReset(attempt_number=2), db, None
    )
    next_attempt = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )

    assert reset.reset is True
    assert db.server.provisioning_attempts == 1
    assert next_attempt.reserved is True
    assert next_attempt.provisioning_attempts == 1


async def test_old_success_cannot_reset_newer_reserved_attempt():
    db = InMemoryAttemptSession(attempts=2)

    reset = await reset_provisioning_attempts(
        "srv-1", ProvisioningAttemptReset(attempt_number=1), db, None
    )

    assert reset.reset is False
    assert db.server.provisioning_attempts == 2
