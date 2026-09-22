"""Tests for the embedding client's per-call HTTP pool."""

import json

import httpx
import pytest

from shared.clients.embedding import MAX_BATCH_SIZE, EmbeddingClient

TIMEOUT = 12.5


def _record_clients(monkeypatch, transport: httpx.MockTransport) -> list[httpx.AsyncClient]:
    """Build real clients against one transport while recording their lifecycle."""
    async_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def create_client(**kwargs) -> httpx.AsyncClient:
        client = async_client(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("shared.clients.embedding.httpx.AsyncClient", create_client)
    return clients


def _embedding_client() -> EmbeddingClient:
    return EmbeddingClient(api_key="test-key", base_url="https://embed.test/v1", timeout=TIMEOUT)


@pytest.mark.asyncio
async def test_generate_shares_one_client_across_batches(monkeypatch):
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        batch_sizes.append(len(texts))
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [float(len(text))]} for text in texts],
                "usage": {"total_tokens": len(texts)},
            },
        )

    clients = _record_clients(monkeypatch, httpx.MockTransport(handler))
    texts = ["x" * (i % 3 + 1) for i in range(MAX_BATCH_SIZE * 2 + 1)]

    result = await _embedding_client().generate(texts)

    assert batch_sizes == [MAX_BATCH_SIZE, MAX_BATCH_SIZE, 1]
    assert result.embeddings == [[float(len(text))] for text in texts]
    assert result.total_tokens == len(texts)
    assert len(clients) == 1
    assert clients[0].is_closed
    assert clients[0].timeout == httpx.Timeout(TIMEOUT)


@pytest.mark.asyncio
async def test_generate_with_no_texts_opens_no_client(monkeypatch):
    clients = _record_clients(monkeypatch, httpx.MockTransport(lambda request: httpx.Response(500)))

    result = await _embedding_client().generate([])

    assert result.embeddings == []
    assert clients == []


def _fail_http_status(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "boom"})


def _fail_transport(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection failed", request=request)


def _fail_format(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"unexpected": True})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (_fail_http_status, httpx.HTTPStatusError),
        (_fail_transport, httpx.ConnectError),
        (_fail_format, ValueError),
    ],
)
async def test_generate_closes_client_when_a_batch_fails(monkeypatch, handler, error):
    requests = 0

    def second_batch_fails(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            texts = json.loads(request.content)["input"]
            return httpx.Response(200, json={"data": [{"embedding": [0.0]} for _ in texts]})
        return handler(request)

    clients = _record_clients(monkeypatch, httpx.MockTransport(second_batch_fails))

    with pytest.raises(error):
        await _embedding_client().generate(["text"] * (MAX_BATCH_SIZE + 1))

    assert requests == 2  # noqa: PLR2004
    assert len(clients) == 1
    assert clients[0].is_closed
