"""Unit tests for API key and SSH key encryption in routers."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    """Clean up FastAPI dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


def _mock_db_session(*, execute_return=None, get_return=None):
    """Build a mock AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.get = AsyncMock(return_value=get_return)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=execute_return)
    session.execute = AsyncMock(return_value=mock_result)

    async def fake_refresh(obj):
        # Simulate DB-assigned defaults that SQLAlchemy only applies on INSERT
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 1
        # Server model columns with DB-level defaults not passed in constructor
        col_defaults = {
            "capacity_disk_mb": 10240,
            "used_ram_mb": 0,
            "used_disk_mb": 0,
            "provisioning_attempts": 0,
        }
        for attr, default in col_defaults.items():
            if hasattr(obj, attr) and getattr(obj, attr, None) is None:
                setattr(obj, attr, default)
        # Timestamps
        now = datetime.now(UTC)
        if not getattr(obj, "created_at", None):
            obj.created_at = now
        if not getattr(obj, "updated_at", None):
            obj.updated_at = now

    session.refresh = AsyncMock(side_effect=fake_refresh)
    return session


def _override_session(session):
    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override


# --- API Key Encryption Tests ---


@pytest.mark.asyncio
async def test_create_api_key_encrypts_value():
    """POST /api-keys/ stores an encrypted (Fernet) value, not plaintext."""
    session = _mock_db_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/api-keys/",
            json={"service": "openai", "value": "sk-test-secret-key"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    # Check the object passed to db.add
    added_obj = session.add.call_args[0][0]
    assert added_obj.key_enc.startswith("gAAAAA"), "key_enc should be a Fernet token"
    assert added_obj.key_enc != "sk-test-secret-key", "key_enc must not be plaintext"


@pytest.mark.asyncio
async def test_create_api_key_updates_existing_encrypted():
    """POST /api-keys/ with existing key updates it with encrypted value."""
    existing_key = MagicMock()
    existing_key.service = "openai"
    existing_key.key_enc = "old-encrypted-value"
    existing_key.type = "system"
    existing_key.project_id = None
    existing_key.id = 1

    session = _mock_db_session(execute_return=existing_key)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/api-keys/",
            json={"service": "openai", "value": "sk-new-secret"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    # Existing key should have been updated with encrypted value
    assert existing_key.key_enc.startswith("gAAAAA"), "Updated key_enc should be Fernet token"
    assert existing_key.key_enc != "sk-new-secret"


@pytest.mark.asyncio
async def test_get_api_key_decrypts_value():
    """GET /api-keys/{service} returns decrypted plaintext."""
    from shared.crypto import SecretsCipher

    cipher = SecretsCipher()
    encrypted = cipher.encrypt("sk-test-secret-key")

    api_key = MagicMock()
    api_key.key_enc = encrypted
    api_key.service = "openai"
    api_key.type = "system"
    api_key.project_id = None

    session = _mock_db_session(execute_return=api_key)
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/api-keys/openai")

    assert resp.status_code == 200  # noqa: PLR2004
    assert resp.json()["value"] == "sk-test-secret-key"


@pytest.mark.asyncio
async def test_get_api_key_rejects_plaintext_value():
    """GET /api-keys/{service} raises InvalidToken on a non-Fernet (plaintext) stored value."""
    from cryptography.fernet import InvalidToken

    api_key = MagicMock()
    api_key.key_enc = "plain-text-legacy-key"  # Not a Fernet token
    api_key.service = "openai"
    api_key.type = "system"
    api_key.project_id = None

    session = _mock_db_session(execute_return=api_key)
    _override_session(session)

    transport = ASGITransport(app=app)
    with pytest.raises(InvalidToken):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/api/api-keys/openai")


# --- Server SSH Key Encryption Tests ---


@pytest.mark.asyncio
async def test_create_server_encrypts_ssh_key():
    """POST /servers/ stores an encrypted SSH key."""
    session = _mock_db_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/servers/",
            json={
                "handle": "srv-1",
                "host": "srv-1.example.com",
                "public_ip": "1.2.3.4",
                "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END-----",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    added_obj = session.add.call_args[0][0]
    assert added_obj.ssh_key_enc.startswith("gAAAAA"), "ssh_key_enc should be a Fernet token"


@pytest.mark.asyncio
async def test_create_server_without_ssh_key():
    """POST /servers/ without ssh_key stores None."""
    session = _mock_db_session()
    _override_session(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/servers/",
            json={
                "handle": "srv-2",
                "host": "srv-2.example.com",
                "public_ip": "5.6.7.8",
            },
        )

    assert resp.status_code == 201  # noqa: PLR2004
    added_obj = session.add.call_args[0][0]
    assert added_obj.ssh_key_enc is None
