"""The CI recovery proof keeps its time bound without a pytest-only plugin."""

import asyncio
import importlib.util
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_native_recovery_proof_cancels_at_its_deadline(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "service/test_initial_owner_retry.py"
    spec = importlib.util.spec_from_file_location("initial_owner_retry_fixture", path)
    recovery = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recovery)
    timeout = asyncio.timeout
    bounds = []
    exited = asyncio.Event()

    def fast_deadline(seconds):
        bounds.append(seconds)
        return timeout(0.01)

    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    monkeypatch.setattr(recovery.asyncio, "timeout", fast_deadline)
    monkeypatch.setattr(recovery, "_native_exhaustion_notice_and_po_retry", blocked, raising=False)
    with pytest.raises(TimeoutError):
        await recovery.test_native_exhaustion_notice_and_po_retry((None,) * 4, None, "retry")
    assert bounds == [180]
    assert exited.is_set()
