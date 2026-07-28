"""Unit tests for telegram_bot settings validation."""

import os

os.environ.setdefault("LK_DOMAIN", "https://lk.test.example.com")

import pytest

from src.config import Settings


def test_blank_lk_domain_is_rejected(monkeypatch):
    """Compose passes an unset LK_DOMAIN through as "" — that must not validate."""
    monkeypatch.setenv("LK_DOMAIN", "")

    with pytest.raises(ValueError, match="LK_DOMAIN|lk_domain"):
        Settings()


def test_lk_domain_is_read_from_env(monkeypatch):
    monkeypatch.setenv("LK_DOMAIN", "https://lk.example.com")

    assert Settings().lk_domain == "https://lk.example.com"
