from datetime import UTC, datetime
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest
from sqlalchemy.sql import Select

from shared.contracts.dto.server import (
    ProvisioningAttemptReservation,
    ProvisioningAttemptReset,
    QATargetReceipt,
    TargetIdentity,
)
from shared.crypto import SecretsCipher
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.ssh_keys import normalize_admin_private_key
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.routers.servers import reserve_provisioning_attempt, reset_provisioning_attempts

FLEET_KEY = fleet_private_key()
IDENTITY = TargetIdentity(
    ssh_user="root",
    host="srv-1.example.test",
    public_ip="203.0.113.1",
    ssh_key_fingerprint=normalize_admin_private_key(FLEET_KEY).fingerprint,
)


def _reset(attempt_number: int, episode_id: str, **receipt) -> ProvisioningAttemptReset:
    return ProvisioningAttemptReset(
        attempt_number=attempt_number,
        episode_id=episode_id,
        qa_target_receipt=QATargetReceipt(
            **{
                "profile_version": QA_TARGET_PROFILE_VERSION,
                "proved_at": datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
                "identity": IDENTITY,
                **receipt,
            }
        ),
    )


async def test_reservation_uses_conditional_atomic_increment():
    db = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = (1, "episode-1")
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
    assert response.episode_id == "episode-1"
    db.commit.assert_awaited_once()


async def test_reservation_at_limit_returns_persisted_count_without_increment():
    db = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = None
    db.execute.return_value = result
    db.get.return_value = MagicMock(provisioning_attempts=3, provisioning_episode_id="episode-1")

    response = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )

    assert response.reserved is False
    assert response.provisioning_attempts == 3
    db.commit.assert_not_awaited()


class InMemoryAttemptSession:
    """One server row, the reservation UPDATE, and the locked reads the reset makes."""

    def __init__(self, attempts: int):
        self.server = SimpleNamespace(
            handle="srv-1",
            provisioning_attempts=attempts,
            provisioning_episode_id=None,
            status="provisioning",
            ssh_user=IDENTITY.ssh_user,
            host=IDENTITY.host,
            public_ip=IDENTITY.public_ip,
            ssh_key_enc=SecretsCipher().encrypt(FLEET_KEY),
            qa_target_version=None,
            qa_target_proved_at=None,
            target_readiness_failure_phase=None,
            target_readiness_parked_status=None,
        )
        self.incidents: list[SimpleNamespace] = []
        self.commits = 0

    async def execute(self, statement):
        result = MagicMock()
        if isinstance(statement, Select):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            wanted = re.search(r"incident_type = '([a-z_]+)'", compiled).group(1)
            matching = [i for i in self.incidents if i.incident_type == wanted]
            result.scalars.return_value.all.return_value = matching
            return result
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        max_attempts = int(compiled.rsplit("<", maxsplit=1)[1].split()[0])
        new_episode_id = re.search(r"THEN '([^']+)'", compiled).group(1)
        value = (
            self.server.provisioning_attempts + 1
            if self.server.provisioning_attempts < max_attempts
            else None
        )
        if value is not None:
            self.server.provisioning_attempts = value
            if value == 1:
                self.server.provisioning_episode_id = new_episode_id
        result.one_or_none.return_value = (
            (value, self.server.provisioning_episode_id) if value is not None else None
        )
        return result

    async def get(self, _model, _handle, **_options):
        return self.server

    async def commit(self):
        self.commits += 1


async def test_successful_episode_resets_persisted_attempts_and_next_reservation_starts_at_one(
    monkeypatch,
):
    db = InMemoryAttemptSession(attempts=2)
    db.server.provisioning_episode_id = "episode-1"
    monkeypatch.setattr("src.routers.servers.uuid4", lambda: "episode-2")

    reset = await reset_provisioning_attempts("srv-1", _reset(2, "episode-1"), db, None)
    next_attempt = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )

    assert reset.reset is True
    assert db.server.provisioning_attempts == 1
    assert db.server.status == "ready"
    assert next_attempt.reserved is True
    assert next_attempt.provisioning_attempts == 1
    assert next_attempt.episode_id == "episode-2"


async def test_the_episode_closes_as_ready_together_with_its_receipt():
    db = InMemoryAttemptSession(attempts=1)
    db.server.provisioning_episode_id = "episode-1"
    readiness = SimpleNamespace(
        incident_type="target_not_ready", status="detected", resolved_at=None, details={}
    )
    software = SimpleNamespace(
        incident_type="provisioning_failed",
        status="detected",
        resolved_at=None,
        details={"step": "software_setup"},
    )
    db.incidents = [readiness, software]
    db.server.target_readiness_failure_phase = "admin_login"

    reset = await reset_provisioning_attempts("srv-1", _reset(1, "episode-1"), db, None)

    assert reset.reset is True
    assert db.server.status == "ready"
    assert db.server.qa_target_version == QA_TARGET_PROFILE_VERSION
    assert db.server.qa_target_proved_at == datetime(2026, 9, 14, 8, 0)
    assert db.server.target_readiness_failure_phase is None
    assert readiness.status == "resolved"
    # A software failure of an earlier attempt is the provisioner's to resolve.
    assert software.status == "detected"
    assert db.commits == 1


@pytest.mark.parametrize(
    "receipt",
    [
        {"identity": IDENTITY.model_copy(update={"ssh_key_fingerprint": "SHA256:another"})},
        {"identity": IDENTITY.model_copy(update={"public_ip": "198.51.100.1"})},
        {"profile_version": "0123456789abcdef"},
    ],
    ids=["another_key", "another_address", "stale_profile"],
)
async def test_a_receipt_for_another_identity_or_profile_closes_nothing(receipt):
    db = InMemoryAttemptSession(attempts=1)
    db.server.provisioning_episode_id = "episode-1"

    with pytest.raises(HTTPException) as refused:
        await reset_provisioning_attempts("srv-1", _reset(1, "episode-1", **receipt), db, None)

    assert refused.value.status_code == 409
    assert db.server.status == "provisioning"
    assert db.server.provisioning_attempts == 1
    assert db.server.qa_target_version is None
    assert db.commits == 0


async def test_old_success_cannot_reset_newer_reserved_attempt():
    db = InMemoryAttemptSession(attempts=2)
    db.server.provisioning_episode_id = "episode-1"

    reset = await reset_provisioning_attempts("srv-1", _reset(1, "episode-1"), db, None)

    assert reset.reset is False
    assert db.server.provisioning_attempts == 2
    # A superseded attempt publishes no receipt.
    assert db.server.qa_target_version is None


async def test_stale_success_cannot_reset_first_attempt_of_a_new_episode(monkeypatch):
    db = InMemoryAttemptSession(attempts=0)
    episodes = iter(["episode-old", "episode-new"])
    monkeypatch.setattr("src.routers.servers.uuid4", lambda: next(episodes))

    old_attempt = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )
    await reset_provisioning_attempts("srv-1", _reset(1, old_attempt.episode_id), db, None)
    new_attempt = await reserve_provisioning_attempt(
        "srv-1", ProvisioningAttemptReservation(max_attempts=3), db, None
    )
    db.server.status = "provisioning"
    db.server.qa_target_version = None
    stale_reset = await reset_provisioning_attempts(
        "srv-1", _reset(1, old_attempt.episode_id), db, None
    )

    assert new_attempt.provisioning_attempts == 1
    assert new_attempt.episode_id == "episode-new"
    assert stale_reset.reset is False
    assert db.server.provisioning_attempts == 1
    assert db.server.provisioning_episode_id == "episode-new"
    assert db.server.status == "provisioning"
    assert db.server.qa_target_version is None
