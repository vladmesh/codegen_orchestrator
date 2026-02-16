"""Unit tests for GitHub webhook HMAC-SHA256 signature verification."""

import hashlib
import hmac

from src.utils.webhook_security import verify_github_signature

SECRET = "test-webhook-secret"  # noqa: S105
PAYLOAD = b'{"action": "completed"}'


def _sign(payload: bytes, secret: str = SECRET) -> str:
    """Generate a valid sha256 signature header."""
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def test_valid_signature():
    sig = _sign(PAYLOAD)
    assert verify_github_signature(PAYLOAD, sig, SECRET) is True


def test_invalid_signature():
    assert verify_github_signature(PAYLOAD, "sha256=badhex", SECRET) is False


def test_missing_header():
    assert verify_github_signature(PAYLOAD, "", SECRET) is False


def test_malformed_header_no_prefix():
    sig = hmac.new(SECRET.encode(), PAYLOAD, hashlib.sha256).hexdigest()
    assert verify_github_signature(PAYLOAD, sig, SECRET) is False


def test_modified_payload():
    sig = _sign(PAYLOAD)
    modified = b'{"action": "modified"}'
    assert verify_github_signature(modified, sig, SECRET) is False
