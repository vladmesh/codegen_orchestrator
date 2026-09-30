"""An empty internal key is never a credential, whatever the settings say."""

from types import SimpleNamespace

import pytest

from src import dependencies
from src.dependencies import is_internal_service


def _configure_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(dependencies, "get_settings", lambda: SimpleNamespace(internal_api_key=key))


@pytest.mark.asyncio
async def test_empty_header_is_refused_even_when_the_configured_key_is_empty(monkeypatch):
    """Settings refuse an empty key at startup; the dependency refuses it again."""
    _configure_key(monkeypatch, "")

    assert await is_internal_service(x_internal_key="") is False


@pytest.mark.asyncio
async def test_empty_header_is_refused_against_a_real_key(monkeypatch):
    _configure_key(monkeypatch, "real-internal-key")

    assert await is_internal_service(x_internal_key="") is False


@pytest.mark.asyncio
async def test_missing_header_is_refused(monkeypatch):
    _configure_key(monkeypatch, "real-internal-key")

    assert await is_internal_service(x_internal_key=None) is False


@pytest.mark.asyncio
async def test_wrong_key_is_refused(monkeypatch):
    _configure_key(monkeypatch, "real-internal-key")

    assert await is_internal_service(x_internal_key="other-key") is False


@pytest.mark.asyncio
async def test_correct_key_is_accepted(monkeypatch):
    _configure_key(monkeypatch, "real-internal-key")

    assert await is_internal_service(x_internal_key="real-internal-key") is True
