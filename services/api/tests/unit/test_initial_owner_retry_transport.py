"""Pin the ASGI cancellation boundary used by the CI rollback proof."""

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from src.main import correlation_middleware


@pytest.mark.asyncio
async def test_pre_response_cancellation_has_the_native_transport_error():
    app = FastAPI()
    app.middleware("http")(correlation_middleware)
    exited = asyncio.Event()

    @app.post("/retry")
    async def cancelled_admission():
        try:
            raise asyncio.CancelledError()
        finally:
            exited.set()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match=r"^No response returned\.$"):
            await client.post("/retry")
    assert exited.is_set()
