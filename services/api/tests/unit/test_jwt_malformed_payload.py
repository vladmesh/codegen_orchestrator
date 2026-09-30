"""Malformed signed JWT payloads must stop at authentication, before database access."""

import base64
import hashlib
import hmac
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest

from src.dependencies import get_lk_user

SECRET = "unit-test-jwt-secret-with-at-least-32-bytes"


def _signed_token(payload: bytes) -> str:
    def segment(value: bytes) -> bytes:
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    message = segment(b'{"alg":"HS256","typ":"JWT"}') + b"." + segment(payload)
    signature = hmac.new(SECRET.encode(), message, hashlib.sha256).digest()
    return (message + b"." + segment(signature)).decode()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b'{"sub":"1","exp":[]}',
        b'{"sub":"1","nbf":{}}',
        b'{"sub":"1","iat":null}',
    ],
    ids=["deeply-nested", "list-exp", "object-nbf", "null-iat"],
)
async def test_malformed_signed_payload_is_unauthorized(payload, monkeypatch):
    if payload is None:
        # Pytest plugins may raise the recursion limit after collection.
        depth = max(10000, sys.getrecursionlimit() + 100)
        payload = b'{"sub":"1","nested":' + b"[" * depth + b"0" + b"]" * depth + b"}"
    monkeypatch.setattr(
        "src.dependencies.get_settings", lambda: SimpleNamespace(lk_jwt_secret=SECRET)
    )
    session = AsyncMock()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_signed_token(payload))

    with pytest.raises(HTTPException) as rejected:
        await get_lk_user(credentials=credentials, db=session)

    assert rejected.value.status_code == 401
    assert rejected.value.detail == "Invalid or expired token"
    session.execute.assert_not_awaited()
