"""RAG is gone from the API: its former routes are not served, even to a trusted caller."""

from http import HTTPStatus

from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from src.main import app

FORMER_RAG_ROUTES = [
    ("POST", "/api/rag/messages"),
    ("POST", "/api/rag/query"),
    ("GET", "/api/rag/summaries"),
    ("POST", "/api/rag/ingest"),
]


def test_no_served_route_lives_under_rag():
    paths = [
        context.path
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
    ]

    assert paths, "the route table is empty; the check below would pass vacuously"
    assert not [path for path in paths if path.startswith("/api/rag")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), FORMER_RAG_ROUTES, ids=lambda v: str(v))
async def test_a_former_rag_route_answers_not_found(method, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.request(method, path, json={}, headers=INTERNAL_HEADERS)

    assert resp.status_code == HTTPStatus.NOT_FOUND, resp.text
