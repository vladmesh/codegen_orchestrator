"""Unit tests for the server SSH key boundary and the target-readiness verdict endpoint."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import ASGITransport, AsyncClient
import pytest

from shared.crypto import SecretsCipher
from shared.qa_target_profile import QA_TARGET_PROFILE_VERSION
from shared.ssh_keys import normalize_admin_private_key
from src.database import get_async_session
from src.main import app

HEADERS = {"X-Internal-Key": "test-internal-key"}


def _fleet_key() -> str:
    """An unencrypted OpenSSH private key, as the fleet's `ssh-keygen` writes one."""
    text = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return text if text.endswith("\n") else text + "\n"


def _locked_key() -> str:
    return (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"a passphrase"),
        )
        .decode()
    )


def _truncated(key: str) -> str:
    lines = key.splitlines()
    return "\n".join([lines[0], lines[1][:20], lines[-1]]) + "\n"


def _mock_server(handle="srv-1", ssh_key_enc=None, **overrides):
    """Create a mock Server ORM object."""
    server = MagicMock()
    server.handle = handle
    server.host = "vps1.example.com"
    server.public_ip = "1.2.3.4"
    server.ssh_user = "root"
    server.ssh_key_enc = ssh_key_enc
    server.ssh_key_fingerprint = None
    server.qa_target_version = None
    server.qa_target_proved_at = None
    server.target_readiness_failure_phase = None
    server.target_readiness_parked_status = None
    server.status = "ready"
    server.is_managed = True
    server.labels = {}
    server.capacity_cpu = 1
    server.capacity_ram_mb = 1024
    server.capacity_disk_mb = 10240
    server.used_ram_mb = 0
    server.used_disk_mb = 0
    server.os_template = None
    server.last_health_check = None
    server.provisioning_started_at = None
    server.provisioning_attempts = 0
    server.provisioning_episode_id = None
    server.notes = None
    server.provider = None
    server.provider_id = None
    server.created_at = datetime.now(UTC)
    server.updated_at = datetime.now(UTC)
    for name, value in overrides.items():
        setattr(server, name, value)
    return server


def _mock_session(server=None):
    """Create a mock DB session."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=server)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()

    async def _session_gen():
        yield session

    return session, _session_gen


@pytest.fixture
def client_for():
    """Bind the app to one mock session for the duration of a test."""

    def bind(session_gen):
        app.dependency_overrides[get_async_session] = session_gen
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield bind
    app.dependency_overrides.clear()


class TestGetServerSSHKey:
    """Test GET /servers/{handle}/ssh-key endpoint."""

    @pytest.mark.asyncio
    async def test_returns_decrypted_key(self, client_for):
        """Server with ssh_key_enc → 200 + decrypted key."""
        raw_key = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "fake-key-content\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        server = _mock_server(ssh_key_enc=SecretsCipher().encrypt(raw_key))
        _, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.get("/api/servers/srv-1/ssh-key", headers=HEADERS)

        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json()["ssh_key"] == raw_key

    @pytest.mark.asyncio
    async def test_no_key_stored_returns_404(self, client_for):
        """Server without ssh_key_enc → 404."""
        _, session_gen = _mock_session(server=_mock_server(ssh_key_enc=None))

        async with client_for(session_gen) as client:
            resp = await client.get("/api/servers/srv-1/ssh-key", headers=HEADERS)

        assert resp.status_code == 404  # noqa: PLR2004
        assert "No SSH key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_server_not_found_returns_404(self, client_for):
        """Non-existent server → 404."""
        _, session_gen = _mock_session(server=None)

        async with client_for(session_gen) as client:
            resp = await client.get("/api/servers/nonexistent/ssh-key", headers=HEADERS)

        assert resp.status_code == 404  # noqa: PLR2004


class TestPatchServerSSHKey:
    """Test PATCH /servers/{handle} with ssh_key field."""

    @pytest.mark.asyncio
    async def test_patch_ssh_key_encrypts_and_stores(self, client_for):
        """PATCH with ssh_key → encrypts and stores in ssh_key_enc."""
        key = _fleet_key()
        server = _mock_server()
        _, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch("/api/servers/srv-1", headers=HEADERS, json={"ssh_key": key})

        assert resp.status_code == 200  # noqa: PLR2004
        # Verify that ssh_key_enc was set (encrypted value, not raw)
        assert server.ssh_key_enc is not None
        assert server.ssh_key_enc != key
        assert SecretsCipher().decrypt(server.ssh_key_enc) == key
        assert server.ssh_key_fingerprint == normalize_admin_private_key(key).fingerprint

    @pytest.mark.asyncio
    async def test_a_replacement_key_replaces_material_and_fingerprint(self, client_for):
        old, new = _fleet_key(), _fleet_key()
        server = _mock_server(
            ssh_key_enc=SecretsCipher().encrypt(old),
            ssh_key_fingerprint=normalize_admin_private_key(old).fingerprint,
        )
        _, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"ssh_key": new.replace("\n", "\r\n")},
            )

        assert resp.status_code == 200  # noqa: PLR2004
        assert SecretsCipher().decrypt(server.ssh_key_enc) == new
        assert resp.json()["ssh_key_fingerprint"] == normalize_admin_private_key(new).fingerprint
        assert new.splitlines()[1] not in resp.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("variant", "reason"),
        [
            ("malformed", "not_openssh_private_key"),
            ("truncated", "malformed"),
            ("no_newline", "no_terminal_newline"),
            ("locked", "not_openssh_private_key"),
            ("empty", "empty"),
            ("null", "empty"),
        ],
    )
    async def test_a_refused_key_changes_nothing_on_the_row(self, client_for, variant, reason):
        """The key is judged before any field of the request is applied, and never echoed."""
        old = _fleet_key()
        fleet = _fleet_key()
        submitted = {
            "malformed": "my-secret-key",
            "truncated": _truncated(fleet),
            "no_newline": fleet.rstrip("\n"),
            "locked": _locked_key(),
            "empty": "",
            "null": None,
        }[variant]
        server = _mock_server(ssh_key_enc=SecretsCipher().encrypt(old), status="ready", notes=None)
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"ssh_key": submitted, "status": "reserved", "notes": "rotated"},
            )

        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["detail"] == f"ssh_key rejected: {reason}"
        if submitted:
            assert (
                submitted.strip().splitlines()[min(1, len(submitted.strip().splitlines()) - 1)]
                not in resp.text
            )
        assert SecretsCipher().decrypt(server.ssh_key_enc) == old
        assert server.status == "ready"
        assert server.notes is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unmanaged_row_may_drop_its_key(self, client_for):
        server = _mock_server(ssh_key_enc=SecretsCipher().encrypt(_fleet_key()), is_managed=False)
        _, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch("/api/servers/srv-1", headers=HEADERS, json={"ssh_key": None})

        assert resp.status_code == 200  # noqa: PLR2004
        assert server.ssh_key_enc is None
        assert server.ssh_key_fingerprint is None


class TestCreateServerSSHKey:
    """POST /servers/ parses the key before the row exists."""

    @staticmethod
    def _body(**overrides) -> dict:
        body = {"handle": "vps-new", "host": "203.0.113.5", "public_ip": "203.0.113.5"}
        body.update(overrides)
        return body

    @pytest.mark.asyncio
    async def test_create_keeps_only_encrypted_material_and_the_fingerprint(self, client_for):
        key = _fleet_key()
        session, session_gen = _mock_session(server=None)

        async def _refresh(row):
            now = datetime.now(UTC)
            row.created_at, row.updated_at = now, now
            for name in (
                "used_ram_mb",
                "used_disk_mb",
                "capacity_disk_mb",
                "provisioning_attempts",
            ):
                if getattr(row, name, None) is None:
                    setattr(row, name, 0)

        session.refresh.side_effect = _refresh

        async with client_for(session_gen) as client:
            resp = await client.post("/api/servers/", headers=HEADERS, json=self._body(ssh_key=key))

        assert resp.status_code == 201, resp.text  # noqa: PLR2004
        row = session.add.call_args.args[0]
        assert SecretsCipher().decrypt(row.ssh_key_enc) == key
        assert row.ssh_key_fingerprint == normalize_admin_private_key(key).fingerprint
        assert resp.json()["ssh_key_fingerprint"] == row.ssh_key_fingerprint
        assert key.splitlines()[1] not in resp.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "submitted", ["", "not a key", "-----BEGIN OPENSSH PRIVATE KEY-----\n"]
    )
    async def test_create_refuses_an_unusable_key_before_adding_a_row(self, client_for, submitted):
        session, session_gen = _mock_session(server=None)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/", headers=HEADERS, json=self._body(ssh_key=submitted)
            )

        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["detail"].startswith("ssh_key rejected: ")
        session.add.assert_not_called()
        session.commit.assert_not_awaited()


class TestTheManagedKeyRule:
    """A managed row holds a key unless provisioning still owns it and will mint one."""

    @staticmethod
    def _created(session) -> None:
        async def _refresh(row):
            now = datetime.now(UTC)
            row.created_at, row.updated_at = now, now
            for name in (
                "used_ram_mb",
                "used_disk_mb",
                "capacity_disk_mb",
                "provisioning_attempts",
            ):
                if getattr(row, name, None) is None:
                    setattr(row, name, 0)

        session.refresh.side_effect = _refresh

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [None, "ready", "active", "error"])
    async def test_a_managed_row_is_not_created_without_a_key(self, client_for, status):
        """The schema defaults — managed, `discovered` — are a row that needs a key."""
        session, session_gen = _mock_session(server=None)
        body = {"handle": "vps-new", "host": "203.0.113.5", "public_ip": "203.0.113.5"}
        if status:
            body["status"] = status

        async with client_for(session_gen) as client:
            resp = await client.post("/api/servers/", headers=HEADERS, json=body)

        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["detail"] == "ssh_key rejected: empty"
        session.add.assert_not_called()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            # Provider discovery: provisioning mints this row's key.
            {"is_managed": True, "status": "pending_setup"},
            {"is_managed": False, "status": "ready"},
        ],
        ids=["discovered_for_provisioning", "unmanaged"],
    )
    async def test_rows_whose_key_is_not_owed_yet_are_created(self, client_for, body):
        session, session_gen = _mock_session(server=None)
        self._created(session)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/",
                headers=HEADERS,
                json={"handle": "vps-new", "host": "h", "public_ip": "203.0.113.5", **body},
            )

        assert resp.status_code == 201, resp.text  # noqa: PLR2004
        assert session.add.call_args.args[0].ssh_key_enc is None

    @pytest.mark.asyncio
    async def test_a_keyless_row_is_not_promoted_into_a_managed_state_that_needs_one(
        self, client_for
    ):
        server = _mock_server(is_managed=False, status="ready", ssh_key_enc=None)
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"is_managed": True, "notes": "adopted"},
            )

        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["detail"] == "ssh_key rejected: empty"
        assert server.is_managed is False
        assert server.notes is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promotion_cannot_clear_the_key_in_the_same_request(self, client_for):
        """The rule is judged on the row the request leaves, not on the row it found."""
        key = _fleet_key()
        server = _mock_server(
            is_managed=False, status="ready", ssh_key_enc=SecretsCipher().encrypt(key)
        )
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"is_managed": True, "ssh_key": None},
            )

        assert resp.status_code == 422  # noqa: PLR2004
        assert server.is_managed is False
        assert SecretsCipher().decrypt(server.ssh_key_enc) == key
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowlist_adoption_of_a_reserved_row_is_still_allowed(self, client_for):
        """server_sync promotes inventory rows as `reserved`; provisioning keys them later."""
        server = _mock_server(is_managed=False, status="reserved", ssh_key_enc=None)
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1", headers=HEADERS, json={"is_managed": True}
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.is_managed is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["pending_setup", "provisioning", "reserved"])
    async def test_an_already_managed_keyless_row_cannot_reach_a_complete_phase(
        self, client_for, status
    ):
        """The fresh-provisioning hole: labels saying `complete` on a row with no key."""
        server = _mock_server(status=status, ssh_key_enc=None, labels={})
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"labels": {"provisioning_phase": "complete"}, "notes": "done"},
            )

        assert resp.status_code == 422  # noqa: PLR2004
        assert resp.json()["detail"] == "ssh_key rejected: empty"
        assert server.labels == {}
        assert server.notes is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_complete_phase_is_accepted_together_with_its_key(self, client_for):
        key = _fleet_key()
        server = _mock_server(status="provisioning", ssh_key_enc=None, labels={})
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1",
                headers=HEADERS,
                json={"ssh_key": key, "labels": {"provisioning_phase": "complete"}},
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.labels == {"provisioning_phase": "complete"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["error", "unreachable"])
    async def test_a_failure_status_on_a_keyless_provisioning_row_is_still_recorded(
        self, client_for, status
    ):
        """Provisioning failures and sync must keep recording what happened to such a row."""
        server = _mock_server(status="provisioning", ssh_key_enc=None, labels={})
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1", headers=HEADERS, json={"status": status}
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.status == status


RECEIPT_AT = datetime(2026, 9, 13, 21, 0)


class TestTheReceiptBelongsToTheConnectionItWasProvedOver:
    @staticmethod
    def _proved(**overrides):
        key = _fleet_key()
        server = _mock_server(
            ssh_key_enc=SecretsCipher().encrypt(key),
            ssh_key_fingerprint=normalize_admin_private_key(key).fingerprint,
            qa_target_version=QA_TARGET_PROFILE_VERSION,
            qa_target_proved_at=RECEIPT_AT,
            **overrides,
        )
        return server, key

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [
            {"ssh_user": "admin"},
            {"host": "vps1-renamed.example.com"},
            # What server_sync writes when the provider reports a new address.
            {"public_ip": "5.6.7.8"},
            {"ssh_key": "new"},
        ],
        ids=["ssh_user", "host", "public_ip", "ssh_key"],
    )
    async def test_a_change_to_the_proved_identity_clears_the_receipt(self, client_for, change):
        server, _ = self._proved()
        session, session_gen = _mock_session(server=server)
        if change.get("ssh_key") == "new":
            change = {"ssh_key": _fleet_key()}

        async with client_for(session_gen) as client:
            resp = await client.patch("/api/servers/srv-1", headers=HEADERS, json=change)

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.qa_target_version is None
        assert server.qa_target_proved_at is None
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [{"notes": "maintenance window"}, {"public_ip": "1.2.3.4"}, {"ssh_key": "same"}],
        ids=["unrelated_field", "same_address", "same_key"],
    )
    async def test_a_write_that_leaves_the_identity_as_it_was_keeps_the_receipt(
        self, client_for, change
    ):
        server, key = self._proved()
        session, session_gen = _mock_session(server=server)
        if change.get("ssh_key") == "same":
            change = {"ssh_key": key}

        async with client_for(session_gen) as client:
            resp = await client.patch("/api/servers/srv-1", headers=HEADERS, json=change)

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.qa_target_version == QA_TARGET_PROFILE_VERSION
        assert server.qa_target_proved_at == RECEIPT_AT

    @pytest.mark.asyncio
    async def test_any_status_write_ends_a_readiness_parks_ownership(self, client_for):
        """Even `error` over `error`: the provisioner now owns that status, not the park."""
        server, _ = self._proved(status="error", target_readiness_parked_status="in_use")
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.patch(
                "/api/servers/srv-1", headers=HEADERS, json={"status": "error"}
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.target_readiness_parked_status is None


def _result(*rows):
    """A `session.execute` result whose `scalars()` answers `first()` and `all()`."""
    scalars = MagicMock()
    scalars.first.return_value = rows[0] if rows else None
    scalars.all.return_value = list(rows)
    result = MagicMock()
    result.scalars.return_value = scalars
    return result


def _incident(incident_type: str, step: str, **overrides) -> SimpleNamespace:
    base = {
        "id": 77,
        "incident_type": incident_type,
        "status": "detected",
        "resolved_at": None,
        "details": {"step": step},
        "recovery_attempts": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_finalizer_validation_never_echoes_generated_private_key(client_for):
    secret = "-----BEGIN OPENSSH PRIVATE KEY-----\nTOP-SECRET\n-----END OPENSSH PRIVATE KEY-----\n"  # noqa: S105 - redaction fixture
    identity = {
        "ssh_user": "root",
        "host": "vps1.example.com",
        "public_ip": "1.2.3.4",
        "ssh_key_fingerprint": "SHA256:proved",
    }
    _, session_gen = _mock_session(server=_mock_server())

    async with client_for(session_gen) as client:
        response = await client.post(
            "/api/servers/srv-1/provisioning/finalize",
            headers=HEADERS,
            json={
                "attempt_number": 1,
                "episode_id": "episode-1",
                "expected_identity": identity | {"ssh_key_fingerprint": None},
                "proved_identity": identity,
                "generated_key_fingerprint": "SHA256:different",
                "generated_private_key": secret,
                "complete_labels": {
                    "provisioning_phase": "complete",
                    "qa_ssh_user": "qa-observer",
                },
                "qa_target_receipt": {
                    "profile_version": QA_TARGET_PROFILE_VERSION,
                    "proved_at": datetime.now(UTC).isoformat(),
                    "identity": identity,
                },
            },
        )

    assert response.status_code == 422  # noqa: PLR2004
    assert secret not in response.text
    assert "TOP-SECRET" not in response.text


class TestTargetReadinessVerdict:
    """POST /servers/{handle}/target-readiness applies one verdict in one transaction."""

    @staticmethod
    def _target(**overrides):
        key = _fleet_key()
        server = _mock_server(ssh_key_enc=SecretsCipher().encrypt(key), **overrides)
        identity = {
            "ssh_user": "root",
            "host": "vps1.example.com",
            "public_ip": "1.2.3.4",
            "ssh_key_fingerprint": normalize_admin_private_key(key).fingerprint,
        }
        return server, identity

    @staticmethod
    def _ready(identity: dict, **overrides) -> dict:
        body = {
            "ready": True,
            "profile_version": QA_TARGET_PROFILE_VERSION,
            "proved_at": datetime(2026, 9, 13, 21, 0, tzinfo=UTC).isoformat(),
            "revision": "c" * 40,
            "identity": identity,
        }
        body.update(overrides)
        return body

    @staticmethod
    def _failed(identity: dict, phase: str = "admin_login") -> dict:
        return {
            "ready": False,
            "phase": phase,
            "detail": "Timeout after 180s",
            "identity": identity,
        }

    @pytest.mark.asyncio
    async def test_a_ready_verdict_restores_only_the_status_its_own_park_took(self, client_for):
        server, identity = self._target(
            status="error",
            target_readiness_parked_status="in_use",
            target_readiness_failure_phase="admin_login",
        )
        session, session_gen = _mock_session(server=server)
        readiness = _incident(
            "target_not_ready", "target_readiness", details={"identity": identity}
        )
        software = _incident("provisioning_failed", "software_setup", id=5)
        qa_identity = _incident(
            "provisioning_failed",
            "qa_identity",
            id=6,
            details={"step": "qa_identity", "server_ip": identity["public_ip"]},
        )
        session.execute = AsyncMock(
            side_effect=[_result(readiness), _result(software, qa_identity)]
        )

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness", headers=HEADERS, json=self._ready(identity)
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.qa_target_version == QA_TARGET_PROFILE_VERSION
        assert server.status == "in_use"
        assert server.target_readiness_parked_status is None
        assert server.target_readiness_failure_phase is None
        assert readiness.status == "resolved"
        # The QA runtime's refusal of this identity is what was repaired...
        assert qa_identity.status == "resolved"
        # ...and an unrelated provisioning failure is not.
        assert software.status == "detected"
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_ready_verdict_never_releases_an_error_it_does_not_own(self, client_for):
        """A generic provisioning or recovery error stays: QA readiness is not its repair."""
        server, identity = self._target(status="error", target_readiness_parked_status=None)
        session, session_gen = _mock_session(server=server)
        software = _incident("provisioning_failed", "software_setup", id=5)
        session.execute = AsyncMock(side_effect=[_result(), _result(software)])

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness", headers=HEADERS, json=self._ready(identity)
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.status == "error"
        assert software.status == "detected"
        assert server.qa_target_version == QA_TARGET_PROFILE_VERSION

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "drift",
        [
            {"ssh_key_fingerprint": "SHA256:another-key"},
            {"public_ip": "5.6.7.8"},
            {"host": "elsewhere.example.com"},
            {"ssh_user": "admin"},
        ],
        ids=["key", "address", "host", "user"],
    )
    async def test_a_verdict_for_another_identity_changes_nothing(self, client_for, drift):
        """The race: the row's identity changed while the reconciliation was running."""
        server, identity = self._target(status="ready")
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            ready = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._ready({**identity, **drift}),
            )
            failed = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._failed({**identity, **drift}),
            )

        assert ready.status_code == failed.status_code == 409  # noqa: PLR2004
        assert server.qa_target_version is None
        assert server.status == "ready"
        assert server.target_readiness_failure_phase is None
        session.execute.assert_not_awaited()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_receipt_for_another_profile_is_refused(self, client_for):
        server, identity = self._target(status="ready")
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._ready(identity, profile_version="0123456789abcdef"),
            )

        assert resp.status_code == 409  # noqa: PLR2004
        assert server.qa_target_version is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_verdict_parks_an_admitting_row_under_its_own_incident(self, client_for):
        server, identity = self._target(
            status="in_use",
            qa_target_version=QA_TARGET_PROFILE_VERSION,
            qa_target_proved_at=RECEIPT_AT,
        )
        encrypted = server.ssh_key_enc
        session, session_gen = _mock_session(server=server)
        session.execute = AsyncMock(return_value=_result())

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._failed(identity, phase="ssh_key_invalid"),
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.status == "error"
        assert server.target_readiness_parked_status == "in_use"
        assert server.target_readiness_failure_phase == "ssh_key_invalid"
        assert server.qa_target_version is None
        assert server.qa_target_proved_at is None
        assert server.ssh_key_enc == encrypted
        incident = session.add.call_args.args[0]
        assert incident.incident_type == "target_not_ready"
        assert incident.details["phase"] == "ssh_key_invalid"
        # Only its own incident was read: no other failure is touched on the way.
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["unreachable", "reserved", "error", "missing"])
    async def test_a_failed_verdict_leaves_a_non_admitting_status_as_it_was(
        self, client_for, status
    ):
        server, identity = self._target(status=status)
        session, session_gen = _mock_session(server=server)
        session.execute = AsyncMock(return_value=_result())

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness", headers=HEADERS, json=self._failed(identity)
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.status == status
        assert server.target_readiness_parked_status is None
        assert server.target_readiness_failure_phase == "admin_login"

    @pytest.mark.asyncio
    async def test_a_repeated_failure_updates_its_one_incident_and_keeps_its_park(self, client_for):
        server, identity = self._target(
            status="error",
            target_readiness_parked_status="ready",
            target_readiness_failure_phase="admin_login",
        )
        session, session_gen = _mock_session(server=server)
        readiness = _incident("target_not_ready", "target_readiness", recovery_attempts=1)
        session.execute = AsyncMock(return_value=_result(readiness))

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._failed(identity, phase="privilege_preflight"),
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert resp.json()["incident_id"] == 77  # noqa: PLR2004
        assert readiness.recovery_attempts == 2  # noqa: PLR2004
        assert readiness.details["phase"] == "privilege_preflight"
        assert server.target_readiness_parked_status == "ready"
        session.add.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"ready": False, "profile_version": QA_TARGET_PROFILE_VERSION, "phase": "admin_login"},
            {"ready": False},
            {"ready": True, "phase": "admin_login"},
            {"ready": False, "phase": "admin_login", "revision": "main"},
        ],
    )
    async def test_a_verdict_is_one_shape_or_none(self, client_for, body):
        session, session_gen = _mock_session(server=_mock_server())

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness", headers=HEADERS, json=body
            )

        assert resp.status_code == 422  # noqa: PLR2004
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unmanaged_row_carries_no_readiness(self, client_for):
        server, identity = self._target(is_managed=False)
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json=self._failed(identity),
            )

        assert resp.status_code == 409  # noqa: PLR2004
        session.commit.assert_not_awaited()
