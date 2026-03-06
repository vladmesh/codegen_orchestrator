"""Unit tests for server SSH key endpoints (GET ssh-key, PATCH with ssh_key)."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _mock_server(handle="srv-1", ssh_key_enc=None):
    """Create a mock Server ORM object."""
    server = MagicMock()
    server.handle = handle
    server.host = "vps1.example.com"
    server.public_ip = "1.2.3.4"
    server.ssh_user = "root"
    server.ssh_key_enc = ssh_key_enc
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
    server.provider_id = None
    return server


def _mock_session(server=None):
    """Create a mock DB session."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=server)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    async def _session_gen():
        yield session

    return session, _session_gen


class TestGetServerSSHKey:
    """Test GET /servers/{handle}/ssh-key endpoint."""

    @pytest.mark.asyncio
    async def test_returns_decrypted_key(self):
        """Server with ssh_key_enc → 200 + decrypted key."""
        from shared.crypto import SecretsCipher

        cipher = SecretsCipher()
        raw_key = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            "fake-key-content\n"
            "-----END OPENSSH PRIVATE KEY-----"
        )
        encrypted = cipher.encrypt(raw_key)

        server = _mock_server(ssh_key_enc=encrypted)
        session, session_gen = _mock_session(server=server)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/servers/srv-1/ssh-key")

            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["ssh_key"] == raw_key
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_no_key_stored_returns_404(self):
        """Server without ssh_key_enc → 404."""
        server = _mock_server(ssh_key_enc=None)
        session, session_gen = _mock_session(server=server)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/servers/srv-1/ssh-key")

            assert resp.status_code == 404  # noqa: PLR2004
            assert "No SSH key" in resp.json()["detail"]
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_server_not_found_returns_404(self):
        """Non-existent server → 404."""
        session, session_gen = _mock_session(server=None)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/servers/nonexistent/ssh-key")

            assert resp.status_code == 404  # noqa: PLR2004
        finally:
            app.dependency_overrides.clear()


class TestPatchServerSSHKey:
    """Test PATCH /servers/{handle} with ssh_key field."""

    @pytest.mark.asyncio
    async def test_patch_ssh_key_encrypts_and_stores(self):
        """PATCH with ssh_key → encrypts and stores in ssh_key_enc."""
        server = _mock_server()
        session, session_gen = _mock_session(server=server)

        app.dependency_overrides[get_async_session] = session_gen

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.patch(
                    "/api/servers/srv-1",
                    json={"ssh_key": "my-secret-key"},
                )

            assert resp.status_code == 200  # noqa: PLR2004
            # Verify that ssh_key_enc was set (encrypted value, not raw)
            assert server.ssh_key_enc is not None
            assert server.ssh_key_enc != "my-secret-key"  # Should be encrypted

            # Verify it can be decrypted back
            from shared.crypto import SecretsCipher

            cipher = SecretsCipher()
            assert cipher.decrypt(server.ssh_key_enc) == "my-secret-key"
        finally:
            app.dependency_overrides.clear()
