"""The core caller-identity dependency that package routes depend on."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Final

from fastapi import Depends, FastAPI, status
from httpx import AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from codegen_kit import caller_identity
from services.backend.src.app.repositories.user import UserRepository
from services.backend.src.core.settings import get_settings

PROBE_PATH: Final[str] = "/_caller-identity-probe"
CAPABILITY: Final[bytes] = b"X-Identity-Capability"
CHANNEL: Final[bytes] = b"X-User-Channel"
EXTERNAL_ID: Final[bytes] = b"X-User-External-Id"

Headers = list[tuple[bytes, bytes]]


def _headers(
    *,
    with_capability: bool = True,
    capability: str | None = None,
    channel: str | None = "telegram",
    external_id: str | None = "111",
) -> Headers:
    headers: Headers = []
    if with_capability:
        value = capability or get_settings().user_identity_capability
        headers.append((CAPABILITY, value.encode()))
    if channel is not None:
        headers.append((CHANNEL, channel.encode()))
    if external_id is not None:
        headers.append((EXTERNAL_ID, external_id.encode()))
    return headers


def _repeated_capability() -> Headers:
    value = get_settings().user_identity_capability
    return [(CAPABILITY, value.encode()), *_headers()]


CASES: Final[list[tuple[str, Callable[[], Headers], int, str | None]]] = [
    ("no capability", lambda: _headers(with_capability=False), status.HTTP_401_UNAUTHORIZED, None),
    ("wrong capability", lambda: _headers(capability="wrong"), status.HTTP_401_UNAUTHORIZED, None),
    ("two capability headers", _repeated_capability, status.HTTP_401_UNAUTHORIZED, None),
    (
        "non-ascii capability",
        lambda: [(CAPABILITY, b"\xff"), *_headers(with_capability=False)],
        status.HTTP_401_UNAUTHORIZED,
        None,
    ),
    ("missing channel", lambda: _headers(channel=None), status.HTTP_401_UNAUTHORIZED, None),
    ("empty channel", lambda: _headers(channel=""), status.HTTP_401_UNAUTHORIZED, None),
    (
        "missing external id",
        lambda: _headers(external_id=None),
        status.HTTP_401_UNAUTHORIZED,
        None,
    ),
    ("empty external id", lambda: _headers(external_id=""), status.HTTP_401_UNAUTHORIZED, None),
    (
        "two channel headers",
        lambda: [(CHANNEL, b"telegram"), *_headers()],
        status.HTTP_401_UNAUTHORIZED,
        None,
    ),
    (
        "ambiguous channel",
        lambda: _headers(channel="telegram:111", external_id="111"),
        status.HTTP_401_UNAUTHORIZED,
        None,
    ),
    (
        "unknown identity",
        lambda: _headers(external_id="999"),
        status.HTTP_403_FORBIDDEN,
        None,
    ),
    (
        "inactive user",
        lambda: _headers(external_id="222"),
        status.HTTP_403_FORBIDDEN,
        None,
    ),
    ("active user", _headers, status.HTTP_200_OK, "telegram:111"),
]


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    """Mount one route that, like a package route, only depends on the core identity."""

    async def probe(user_ref: Annotated[str, Depends(caller_identity)]) -> dict[str, str]:
        return {"user_ref": user_ref}

    app.add_api_route(PROBE_PATH, probe, methods=["GET"])
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "headers", "expected_status", "expected_ref"),
    CASES,
    ids=[case[0] for case in CASES],
)
async def test_caller_identity_table(
    probe_app: FastAPI,
    client: AsyncClient,
    db_session: AsyncSession,
    case: str,
    headers: Callable[[], Headers],
    expected_status: int,
    expected_ref: str | None,
) -> None:
    users = UserRepository(db_session)
    await users.grant("telegram", "111")
    await users.grant("telegram", "222")
    await users.revoke("telegram", "222")

    response = await client.get(PROBE_PATH, headers=headers())

    assert response.status_code == expected_status, (case, response.text)
    if expected_ref is not None:
        assert response.json() == {"user_ref": expected_ref}
    assert get_settings().user_identity_capability not in response.text


def test_identity_headers_stay_out_of_the_api_contract(probe_app: FastAPI) -> None:
    document = str(probe_app.openapi())

    assert CAPABILITY.decode() not in document
    assert "USER_IDENTITY_CAPABILITY" not in document
