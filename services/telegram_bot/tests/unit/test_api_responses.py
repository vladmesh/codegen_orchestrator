"""API responses enter the bot as typed models, parsed once in the API client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pytest

from src import middleware
from src.clients.api import APIResponseError, TelegramAPIClient
from src.texts import text

PROJECT_ID = str(uuid.uuid4())
PROJECT = {
    "id": PROJECT_ID,
    "title": "Pet Shop",
    "slug": "pet-shop",
    "status": "active",
    "config": {"description": "Sells pets", "repo_url": "https://github.com/o/pet-shop"},
    "owner_id": 7,
    "created_at": "2026-01-01T00:00:00Z",
}
SERVER = {
    "handle": "srv-1",
    "host": "srv-1.example",
    "public_ip": "203.0.113.5",
    "ssh_user": "root",
    "status": "ready",
    "is_managed": True,
    "capacity_ram_mb": 4096,
    "used_ram_mb": 1024,
    "created_at": "2026-01-01T00:00:00Z",
}
USER = {"id": 3, "telegram_id": 42, "is_admin": True, "created_at": "2026-01-01T00:00:00Z"}


@pytest.fixture
def api(monkeypatch):
    """A bot API client whose API answers each path with the JSON set in `routes`."""
    routes: dict[str, object] = {}
    sent: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        path = request.url.path.removeprefix("/api")
        if request.url.query:
            path = f"{path}?{request.url.query.decode()}"
        return httpx.Response(200, json=routes[path])

    def factory(**kwargs):
        return real_async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", factory)
    monkeypatch.setenv("INTERNAL_API_KEY", "bot-test-key")
    with patch("src.clients.api.get_settings") as settings:
        settings.return_value.api_base_url = "http://api:8000"
        client = TelegramAPIClient()
    client.routes = routes
    client.sent = sent
    return client


async def test_a_project_list_is_parsed_and_scoped_to_the_caller(api):
    api.routes["/projects"] = [PROJECT]

    [project] = await api.list_projects(12345)

    assert (str(project.id), project.title, project.status) == (PROJECT_ID, "Pet Shop", "active")
    assert api.sent[-1].headers["X-Telegram-ID"] == "12345"


async def test_a_project_without_a_title_fails_at_the_parse(api):
    api.routes["/projects"] = [{key: v for key, v in PROJECT.items() if key != "title"}]

    with pytest.raises(APIResponseError) as failure:
        await api.list_projects(12345)

    assert failure.value.path == "/projects"
    assert "'loc': [0, 'title']" in str(failure.value)
    assert "Pet Shop" not in str(failure.value)


async def test_a_user_without_a_telegram_id_fails_at_the_parse(api):
    api.routes["/users/by-telegram/42"] = {
        key: v for key, v in USER.items() if key != "telegram_id"
    }

    with (
        patch.object(middleware, "api_client", api),
        pytest.raises(APIResponseError, match="telegram_id"),
    ):
        await middleware._check_user_in_db(42)


async def test_a_registered_user_is_admitted_with_their_admin_flag(api):
    api.routes["/users/by-telegram/42"] = USER
    update = MagicMock()
    update.effective_user.id = 42
    context = MagicMock(user_data={})
    settings = MagicMock()
    settings.get_admin_ids.return_value = set()

    with (
        patch.object(middleware, "api_client", api),
        patch.object(middleware, "get_settings", return_value=settings),
    ):
        assert await middleware.auth_middleware(update, context) is True

    assert context.user_data[middleware.USER_IS_ADMIN_KEY] is True
    assert "X-Telegram-ID" not in api.sent[-1].headers


def _callback(data: str):
    query = AsyncMock()
    query.data = data
    query.from_user.id = 42
    update = MagicMock()
    update.callback_query = query
    update.effective_user.language_code = "en"
    return update, query


async def _press(api, data: str, *, admin: bool = False):
    from src.handlers import handle_callback_query

    update, query = _callback(data)
    with (
        patch("src.handlers.api_client", api),
        patch("src.handlers.is_admin", return_value=admin),
    ):
        await handle_callback_query(update, MagicMock())
    return query.edit_message_text.call_args


async def test_project_details_show_the_api_title_and_config(api):
    api.routes[f"/projects/{PROJECT_ID}"] = PROJECT

    shown = await _press(api, f"project:details:{PROJECT_ID}")

    assert "*Pet Shop*" in shown.args[0]
    assert "Sells pets" in shown.args[0]
    assert "github\\.com" in shown.args[0]


async def test_project_details_without_a_description_say_so(api):
    api.routes[f"/projects/{PROJECT_ID}"] = {**PROJECT, "config": {}}

    shown = await _press(api, f"project:details:{PROJECT_ID}")

    assert text("en", "no_description") in shown.args[0]
    assert text("en", "repo_label") not in shown.args[0]


async def test_the_server_list_shows_typed_server_lines(api):
    api.routes["/servers?is_managed=true"] = [SERVER]

    shown = await _press(api, "servers:list", admin=True)

    assert "✅ *srv\\-1* \\[ready\\] — 203\\.0\\.113\\.5 \\(RAM: 1024/4096 MB\\)" in shown.args[0]


async def test_a_malformed_project_list_never_reaches_the_keyboard(api):
    api.routes["/projects"] = [{key: v for key, v in PROJECT.items() if key != "title"}]

    with patch("src.handlers.projects_list_keyboard") as keyboard:
        shown = await _press(api, "projects:list")

    keyboard.assert_not_called()
    assert shown.args[0] == text("en", "action_failed_md")
