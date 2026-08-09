"""Credential helpers. Tokens are never logged or stored in plaintext."""

import hashlib
import hmac


def credential_key(worker_id: str) -> str:
    return f"worker:broker:{worker_id}"


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, expected_digest: str | None) -> bool:
    return bool(expected_digest) and hmac.compare_digest(token_digest(token), expected_digest)
