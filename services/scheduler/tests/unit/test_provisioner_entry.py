"""Unit tests for the provisioner:results per-entry handler.

Covers the poison-message path introduced when `BaseResult.status` stopped
accepting the legacy `error` synonym: a message that can never validate must be
ACKed away (terminal) instead of reclaimed forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.tasks import provisioner_result_listener as listener
from src.tasks.provisioner_result_listener import handle_provisioner_entry


class FakeClient:
    """Records ACKs so tests can assert terminal handling."""

    def __init__(self) -> None:
        self.acked: list[str] = []
        self.rejected: list[str] = []

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        self.acked.append(message_id)

    async def reject_entry(self, stream, group, message_id, **kwargs) -> None:
        self.rejected.append(message_id)
        self.acked.append(message_id)


def _entry(message_id: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(message_id=message_id, data=data)


async def test_invalid_status_is_quarantined_then_acked(monkeypatch):
    """A legacy/invalid status is terminal but remains visible in the DLQ."""
    processed: list = []

    async def _spy(result):
        processed.append(result)

    monkeypatch.setattr("src.tasks.provisioner_result_listener.process_provisioner_result", _spy)

    client = FakeClient()
    poison = _entry("10-0", {"request_id": "r", "status": "error", "server_handle": "h"})

    await handle_provisioner_entry(client, poison)

    assert client.rejected == ["10-0"]
    assert client.acked == ["10-0"]  # terminal ACK only after quarantine
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


async def test_transient_api_503_is_not_acked(monkeypatch):
    """A server-side API failure remains pending for bounded redelivery."""
    request = httpx.Request("PATCH", "http://api/servers/h")
    response = httpx.Response(503, request=request)

    async def _update(*_args, **_kwargs):
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    monkeypatch.setattr(listener.api_client, "update_server", _update)

    client = FakeClient()
    entry = _entry(
        "11-5",
        {"request_id": "r", "status": "failed", "server_handle": "h", "errors": ["boom"]},
    )

    with pytest.raises(httpx.HTTPStatusError):
        await handle_provisioner_entry(client, entry)

    assert client.acked == []
    assert client.rejected == []


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


async def test_failed_result_is_processed_then_acked(monkeypatch):
    """Terminal notification is owned by the Telegram results consumer, not scheduler."""

    async def _update(server_id, server):
        return None

    monkeypatch.setattr(listener.api_client, "update_server", _update)

    client = FakeClient()
    entry = _entry(
        "13-0",
        {
            "request_id": "r",
            "status": "failed",
            "server_handle": "h",
            "errors": ["provisioning failed"],
        },
    )

    await handle_provisioner_entry(client, entry)

    assert client.acked == ["13-0"]


async def test_superseded_result_causes_no_mutation_or_notification(monkeypatch):
    """A SUPERSEDED result is a no-op: the newer attempt owns the server.

    The stale job must not mutate server status (no flip to UNREACHABLE) and must
    not raise a failure notification.
    """
    from shared.contracts.queues.provisioner import ProvisionerResult
    from shared.contracts.vocab import ResultStatus
    from src.tasks import provisioner_result_listener as listener

    update_calls: list = []

    async def _update(server_id, server):
        update_calls.append((server_id, server))

    monkeypatch.setattr(listener.api_client, "update_server", _update)

    result = ProvisionerResult(
        request_id="r", status=ResultStatus.SUPERSEDED, server_handle="srv-1"
    )
    await listener.process_provisioner_result(result)

    assert update_calls == []  # no status mutation
