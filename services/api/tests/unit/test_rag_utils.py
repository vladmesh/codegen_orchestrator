"""Unit tests for RAG helpers."""

import hashlib
import hmac

from src.routers.rag import _build_signature, _hash_text


def test_build_signature_matches_hmac():
    signing_key = "unit-test-key"
    timestamp = 1700000000
    body = b'{"hello":"world"}'
    expected = hmac.new(
        signing_key.encode("utf-8"),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()

    assert _build_signature(signing_key, timestamp, body) == expected


def test_hash_text_prefix():
    digest = _hash_text("hello")
    assert digest.startswith("sha256:")
