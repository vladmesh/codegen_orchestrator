"""Unit tests for SmokeTesterNode."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.subgraphs.devops.smoke import SmokeTesterNode


@pytest.fixture
def smoke_node():
    return SmokeTesterNode()


def _make_state(
    *,
    modules=None,
    allocated_resources=None,
    deployed_url="http://1.2.3.4:8000",
    resolved_secrets=None,
):
    """Helper to build a minimal DevOpsState dict for smoke tests."""
    if modules is None:
        modules = ["backend"]
    if allocated_resources is None:
        allocated_resources = {
            "srv1:8000": {
                "server_ip": "1.2.3.4",
                "port": 8000,
                "service_name": "backend",
            }
        }
    return {
        "messages": [],
        "project_id": "test-project",
        "project_spec": {"config": {"modules": modules}},
        "allocated_resources": allocated_resources,
        "repo_info": None,
        "provided_secrets": {},
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": resolved_secrets or {},
        "missing_user_secrets": [],
        "deployment_result": {"status": "success"},
        "deployed_url": deployed_url,
        "errors": [],
        "smoke_result": None,
    }


class TestSmokeTesterBackendPass:
    """Backend health check returns 200."""

    async def test_pass_on_200(self, smoke_node):
        state = _make_state()
        mock_response = AsyncMock()
        mock_response.status_code = 200

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        assert len(result["smoke_result"]["checks"]) == 1
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "backend"
        assert check["result"] == "pass"
        assert "errors" not in result or result["errors"] == []


class TestSmokeTesterBackendFail:
    """Backend health check returns non-200."""

    async def test_fail_on_500(self, smoke_node):
        state = _make_state()
        mock_response = AsyncMock()
        mock_response.status_code = 500

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock):
                result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "backend"
        assert check["result"] == "fail"
        assert len(result["errors"]) > 0


class TestSmokeTesterBackendTimeout:
    """Backend health check times out after retries."""

    async def test_timeout(self, smoke_node):
        state = _make_state()

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
            mock_client_cls.return_value = mock_client

            with patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock):
                result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "timeout" in check["detail"].lower() or "connect" in check["detail"].lower()


class TestSmokeTesterRetryLogic:
    """Verify retry logic: fail first, pass on retry."""

    async def test_retries_then_passes(self, smoke_node):
        state = _make_state()

        fail_response = AsyncMock()
        fail_response.status_code = 500
        pass_response = AsyncMock()
        pass_response.status_code = 200

        with patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=[fail_response, pass_response])
            mock_client_cls.return_value = mock_client

            with patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock):
                result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"


class TestSmokeTesterNoModules:
    """No modules to check — vacuous pass."""

    async def test_empty_modules(self, smoke_node):
        state = _make_state(modules=[], allocated_resources={})

        result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        assert result["smoke_result"]["checks"] == []


def _tg_bot_state(**kwargs):
    """Helper for tg_bot smoke tests."""
    return _make_state(
        modules=["tg_bot"],
        allocated_resources={
            "srv1:8001": {
                "server_ip": "1.2.3.4",
                "port": 8001,
                "service_name": "tg_bot",
            }
        },
        resolved_secrets={"TELEGRAM_BOT_TOKEN": "123456:ABC-DEF"},
        **kwargs,
    )


class TestSmokeTesterTgBotPass:
    """Telethon /start gets a response."""

    async def test_pass_on_response(self, smoke_node):
        state = _tg_bot_state()

        # Mock getMe API call
        mock_getme_response = AsyncMock()
        mock_getme_response.status_code = 200
        mock_getme_response.json = MagicMock(
            return_value={"ok": True, "result": {"username": "test_bot"}}
        )

        # Mock Telethon client
        mock_telethon_client = AsyncMock()
        mock_telethon_client.start = AsyncMock()
        mock_telethon_client.send_message = AsyncMock()
        mock_telethon_client.disconnect = AsyncMock()

        # Mock incoming message
        mock_event = MagicMock()
        mock_event.message = MagicMock()
        mock_event.message.text = "Welcome!"

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_http_cls,
            patch("src.subgraphs.devops.smoke.TelegramClient") as mock_tg_cls,
            patch.dict(
                os.environ,
                {
                    "TELETHON_API_ID": "12345",
                    "TELETHON_API_HASH": "abcdef",
                    "TELETHON_SESSION_PATH": "/var/lib/telethon/test.session",
                },
            ),
        ):
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.get = AsyncMock(return_value=mock_getme_response)
            mock_http_cls.return_value = mock_http

            mock_tg_cls.return_value = mock_telethon_client

            # Simulate receiving a message via get_response
            mock_telethon_client.get_response = AsyncMock(return_value=mock_event.message)

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "pass"


class TestSmokeTesterTgBotTimeout:
    """Telethon /start gets no response within timeout."""

    async def test_timeout(self, smoke_node):
        state = _tg_bot_state()

        mock_getme_response = AsyncMock()
        mock_getme_response.status_code = 200
        mock_getme_response.json = MagicMock(
            return_value={"ok": True, "result": {"username": "test_bot"}}
        )

        mock_telethon_client = AsyncMock()
        mock_telethon_client.start = AsyncMock()
        mock_telethon_client.send_message = AsyncMock()
        mock_telethon_client.disconnect = AsyncMock()
        mock_telethon_client.get_response = AsyncMock(side_effect=TimeoutError("no response"))

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_http_cls,
            patch("src.subgraphs.devops.smoke.TelegramClient") as mock_tg_cls,
            patch.dict(
                os.environ,
                {
                    "TELETHON_API_ID": "12345",
                    "TELETHON_API_HASH": "abcdef",
                    "TELETHON_SESSION_PATH": "/var/lib/telethon/test.session",
                },
            ),
        ):
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.get = AsyncMock(return_value=mock_getme_response)
            mock_http_cls.return_value = mock_http

            mock_tg_cls.return_value = mock_telethon_client

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "fail"


class TestSmokeTesterTgBotMissingEnv:
    """Skip tg_bot check if Telethon env vars not configured."""

    async def test_skip_without_env(self, smoke_node):
        state = _tg_bot_state()

        with patch.dict(os.environ, {}, clear=True):
            # Ensure TELETHON_* vars are not set
            for key in ["TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION_PATH"]:
                os.environ.pop(key, None)

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "skip"


# ---------------------------------------------------------------------------
# Container log capture on smoke failure
# ---------------------------------------------------------------------------


def _make_state_with_handle(*, modules=None, server_handle="srv-abc"):
    """State that includes server_handle in allocated_resources + project name."""
    if modules is None:
        modules = ["backend"]
    return {
        "messages": [],
        "project_id": "test-project",
        "project_spec": {"name": "my-cool-project", "config": {"modules": modules}},
        "allocated_resources": {
            "srv-abc:8000": {
                "server_ip": "1.2.3.4",
                "port": 8000,
                "service_name": "backend",
                "server_handle": server_handle,
            }
        },
        "repo_info": None,
        "provided_secrets": {},
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": {},
        "missing_user_secrets": [],
        "deployment_result": {"status": "success"},
        "deployed_url": "http://1.2.3.4:8000",
        "errors": [],
        "smoke_result": None,
    }


class TestContainerLogCapture:
    """When smoke fails, container logs are fetched via SSH and appended to detail."""

    async def test_logs_appended_on_backend_fail(self, smoke_node):
        """Failed backend check → detail includes docker compose logs output."""
        state = _make_state_with_handle()
        mock_response = AsyncMock()
        mock_response.status_code = 500

        mock_ssh_result = MagicMock()
        mock_ssh_result.stdout = (
            "Traceback: ModuleNotFoundError: No module named 'shared.generated'"
        )
        mock_ssh_result.exit_status = 0

        mock_conn = AsyncMock()
        mock_conn.run = AsyncMock(return_value=mock_ssh_result)
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls,
            patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock),
            patch("src.subgraphs.devops.smoke.api_client") as mock_api,
            patch("src.subgraphs.devops.smoke.asyncssh") as mock_asyncssh,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            mock_api.get_server_ssh_key = AsyncMock(return_value="fake-ssh-key")
            mock_asyncssh.import_private_key = MagicMock(return_value="parsed-key")
            mock_asyncssh.connect = MagicMock(return_value=mock_conn)

            result = await smoke_node.run(state)

        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "ModuleNotFoundError" in check["detail"]
        assert "HTTP 500" in check["detail"]

    async def test_logs_not_fetched_on_pass(self, smoke_node):
        """Passing smoke check must NOT trigger SSH log fetch."""
        state = _make_state_with_handle()
        mock_response = AsyncMock()
        mock_response.status_code = 200

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls,
            patch("src.subgraphs.devops.smoke.api_client") as mock_api,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        mock_api.get_server_ssh_key.assert_not_called()

    async def test_logs_ssh_failure_does_not_break_smoke(self, smoke_node):
        """If SSH log fetch fails, smoke still reports the original error."""
        state = _make_state_with_handle()
        mock_response = AsyncMock()
        mock_response.status_code = 502

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls,
            patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock),
            patch("src.subgraphs.devops.smoke.api_client") as mock_api,
            patch("src.subgraphs.devops.smoke.asyncssh") as mock_asyncssh,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            mock_api.get_server_ssh_key = AsyncMock(side_effect=Exception("API down"))
            mock_asyncssh.import_private_key = MagicMock()

            result = await smoke_node.run(state)

        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "HTTP 502" in check["detail"]

    async def test_logs_missing_server_handle_skips_fetch(self, smoke_node):
        """If no server_handle in allocated_resources, skip log fetch gracefully."""
        state = _make_state_with_handle()
        # Remove server_handle
        for alloc in state["allocated_resources"].values():
            alloc.pop("server_handle", None)

        mock_response = AsyncMock()
        mock_response.status_code = 503

        with (
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient") as mock_client_cls,
            patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock),
            patch("src.subgraphs.devops.smoke.api_client") as mock_api,
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await smoke_node.run(state)

        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "HTTP 503" in check["detail"]
        mock_api.get_server_ssh_key.assert_not_called()
