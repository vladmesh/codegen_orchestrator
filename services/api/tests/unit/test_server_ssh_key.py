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


class TestTargetReadinessVerdict:
    """POST /servers/{handle}/target-readiness applies one verdict in one transaction."""

    @pytest.mark.asyncio
    async def test_a_ready_verdict_writes_the_receipt_and_releases_a_parked_row(self, client_for):
        server = _mock_server(status="error")
        session, session_gen = _mock_session(server=server)
        proved_at = datetime(2026, 9, 13, 21, 0, tzinfo=UTC)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json={
                    "ready": True,
                    "profile_version": QA_TARGET_PROFILE_VERSION,
                    "proved_at": proved_at.isoformat(),
                    "revision": "c" * 40,
                },
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert server.qa_target_version == QA_TARGET_PROFILE_VERSION
        assert server.qa_target_proved_at == proved_at.replace(tzinfo=None)
        assert server.status == "ready"
        # The active provisioning-failure episode is resolved in the same transaction.
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_receipt_for_another_profile_is_refused(self, client_for):
        server = _mock_server(status="ready")
        session, session_gen = _mock_session(server=server)

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json={
                    "ready": True,
                    "profile_version": "0123456789abcdef",
                    "proved_at": datetime.now(UTC).isoformat(),
                },
            )

        assert resp.status_code == 409  # noqa: PLR2004
        assert server.qa_target_version is None
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_verdict_parks_the_row_and_keeps_the_encrypted_key(self, client_for):
        encrypted = SecretsCipher().encrypt(_fleet_key())
        server = _mock_server(
            ssh_key_enc=encrypted,
            status="in_use",
            qa_target_version=QA_TARGET_PROFILE_VERSION,
            qa_target_proved_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session, session_gen = _mock_session(server=server)
        session.execute = AsyncMock(
            return_value=SimpleNamespace(scalar_one=lambda: SimpleNamespace(id=77))
        )

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json={"ready": False, "phase": "ssh_key_invalid", "detail": "malformed"},
            )

        assert resp.status_code == 200, resp.text  # noqa: PLR2004
        assert resp.json()["incident_id"] == 77  # noqa: PLR2004
        assert server.status == "error"
        assert server.qa_target_version is None
        assert server.qa_target_proved_at is None
        assert server.ssh_key_enc == encrypted
        session.commit.assert_awaited_once()

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
        session, session_gen = _mock_session(server=_mock_server(is_managed=False))

        async with client_for(session_gen) as client:
            resp = await client.post(
                "/api/servers/srv-1/target-readiness",
                headers=HEADERS,
                json={"ready": False, "phase": "admin_login"},
            )

        assert resp.status_code == 409  # noqa: PLR2004
        session.commit.assert_not_awaited()
