"""Unit tests for the provisioner:results per-entry handler.

Covers the poison-message path introduced when `BaseResult.status` stopped
accepting the legacy `error` synonym: a message that can never validate must be
ACKed away (terminal) instead of reclaimed forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tasks.provisioner_result_listener import handle_provisioner_entry


class FakeClient:
    """Records ACKs so tests can assert terminal handling."""

    def __init__(self) -> None:
        self.acked: list[str] = []

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self.acked.append(message_id)


def _entry(message_id: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, data=data)


async def test_invalid_status_is_acked_and_discarded(monkeypatch):
    """A legacy/invalid status ('error') fails validation and must be ACKed."""
    processed: list = []

    async def _spy(result):
        processed.append(result)

    monkeypatch.setattr("src.tasks.provisioner_result_listener.process_provisioner_result", _spy)

    client = FakeClient()
    poison = _entry("10-0", {"request_id": "r", "status": "error", "server_handle": "h"})

    await handle_provisioner_entry(client, poison)

    assert client.acked == ["10-0"]  # terminal ACK, no reclaim loop
    assert processed == []  # never dispatched downstream


async def test_valid_message_is_processed_then_acked(monkeypatch):
    processed: list = []

    async def _spy(result):
        processed.append(result)

    monkeypatch.setattr("src.tasks.provisioner_result_listener.process_provisioner_result", _spy)

    client = FakeClient()
    entry = _entry("11-0", {"request_id": "r", "status": "success", "server_handle": "h"})

    await handle_provisioner_entry(client, entry)

    assert len(processed) == 1
    assert processed[0].server_handle == "h"
    assert client.acked == ["11-0"]


async def test_processing_error_is_not_acked(monkeypatch):
    """A transient processing failure stays unacked so it gets retried."""

    async def _boom(result):
        raise RuntimeError("api down")

    monkeypatch.setattr("src.tasks.provisioner_result_listener.process_provisioner_result", _boom)

    client = FakeClient()
    entry = _entry("12-0", {"request_id": "r", "status": "success", "server_handle": "h"})

    with pytest.raises(RuntimeError):
        await handle_provisioner_entry(client, entry)

    assert client.acked == []  # left in PEL for retry
