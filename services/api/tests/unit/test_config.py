"""Unit tests for API settings validation."""

import pytest

from src.config import Settings


def test_blank_lk_jwt_secret_is_rejected(monkeypatch):
    """An empty LK_JWT_SECRET would sign dashboard tokens with a known key."""
    monkeypatch.setenv("LK_JWT_SECRET", "")

    with pytest.raises(ValueError, match="LK_JWT_SECRET|lk_jwt_secret"):
        Settings()
