"""Unit tests for SmokeTesterNode."""

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
    secret_values=None,
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
        "project_spec": {
            "title": "Test Project",
            "slug": "test-project-0000",
            "config": {"modules": modules},
        },
        "allocated_resources": allocated_resources,
        "repo_info": None,
        "provided_secrets": {},
        "secret_values": secret_values or {},
        "non_secret_values": {},
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


def _tg_bot_state(*, secret_values=None, server_handle="srv-abc", **kwargs):
    """Helper for tg_bot smoke tests."""
    resource = {
        "server_ip": "1.2.3.4",
        "port": 8001,
        "service_name": "tg_bot",
    }
    if server_handle:
        resource["server_handle"] = server_handle
    if secret_values is None:
        secret_values = {"TELEGRAM_BOT_TOKEN": "123456:ABC-DEF"}
    return _make_state(
        modules=["tg_bot"],
        allocated_resources={"srv-abc:8001": resource},
        secret_values=secret_values,
        **kwargs,
    )


def _getme_response(*, status_code=200, payload=None):
    """Bot API getMe response double."""
    response = AsyncMock()
    response.status_code = status_code
    if payload is None:
        payload = {"ok": True, "result": {"username": "test_bot"}}
    response.json = MagicMock(return_value=payload)
    return response


class _TgBotEnv:
    """Patches httpx + SSH so a tg_bot check runs against fakes."""

    def __init__(self, *, getme, ps_stdout=None, ssh_error=None):
        self.getme = getme
        self.ps_stdout = ps_stdout
        self.ssh_error = ssh_error
        self.conn = AsyncMock()
        self.commands = []

    async def _run(self, command, check=False):
        self.commands.append(command)
        result = MagicMock()
        result.stdout = self.ps_stdout if "ps " in command else "bot log line"
        result.exit_status = 0
        return result

    def __enter__(self):
        http = AsyncMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        if isinstance(self.getme, list):
            http.get = AsyncMock(side_effect=self.getme)
        else:
            http.get = AsyncMock(return_value=self.getme)

        self.conn.run = AsyncMock(side_effect=self._run)
        self.conn.__aenter__ = AsyncMock(return_value=self.conn)
        self.conn.__aexit__ = AsyncMock(return_value=False)

        api = MagicMock()
        api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
        if self.ssh_error:
            api.get_server_ssh_key = AsyncMock(side_effect=self.ssh_error)
        else:
            api.get_server_ssh_key = AsyncMock(return_value="fake-ssh-key")
        self.api = api

        asyncssh_mock = MagicMock()
        asyncssh_mock.import_private_key = MagicMock(return_value="parsed-key")
        asyncssh_mock.connect = MagicMock(return_value=self.conn)

        self._patches = [
            patch("src.subgraphs.devops.smoke.httpx.AsyncClient", return_value=http),
            patch("src.subgraphs.devops.smoke.asyncio.sleep", new_callable=AsyncMock),
            patch("src.subgraphs.devops.smoke.api_client", api),
            patch("src.subgraphs.devops.smoke.asyncssh", asyncssh_mock),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestSmokeTesterTgBotPass:
    """getMe answers and the tg_bot container is running."""

    async def test_pass_on_running_container(self, smoke_node):
        state = _tg_bot_state()

        with _TgBotEnv(getme=_getme_response(), ps_stdout="backend\ndb\nredis\ntg_bot\n") as env:
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "pass"
        assert "test_bot" in check["detail"]
        assert "running" in check["detail"]
        # bot_username reaches the state for the QA handoff
        assert result["bot_username"] == "test_bot"
        ps_cmd = next(c for c in env.commands if "ps " in c)
        assert "docker compose -p test-project-0000" in ps_cmd
        assert "--status running" in ps_cmd


class TestSmokeTesterTgBotGetMeRetry:
    """A transient network error to the Bot API is retried, not reported as a dead bot."""

    async def test_retries_then_passes(self, smoke_node):
        state = _tg_bot_state()
        getme = [httpx.ConnectError("connection reset"), _getme_response()]

        with _TgBotEnv(getme=getme, ps_stdout="tg_bot\n"):
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "pass"
        assert result["bot_username"] == "test_bot"


class TestSmokeTesterTgBotContainerDown:
    """The bot answers on the Bot API but its container is not running."""

    async def test_fail_when_container_missing(self, smoke_node):
        state = _tg_bot_state()

        with _TgBotEnv(getme=_getme_response(), ps_stdout="backend\ndb\nredis\n"):
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "fail"
        assert "not running" in check["detail"]
        assert result["errors"]
        assert "bot_username" not in result


class TestSmokeTesterTgBotBadToken:
    """getMe rejects the token — the bot cannot be confirmed."""

    async def test_fail_on_unauthorized(self, smoke_node):
        state = _tg_bot_state()
        unauthorized = _getme_response(
            status_code=401, payload={"ok": False, "description": "Unauthorized"}
        )

        with _TgBotEnv(getme=unauthorized, ps_stdout="tg_bot\n") as env:
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "Unauthorized" in check["detail"]
        # No container probe once the identity probe failed
        assert not [c for c in env.commands if "ps " in c]


class TestSmokeTesterTgBotSshFailure:
    """SSH is unavailable — report it, never pass the bot silently."""

    async def test_fail_when_ssh_breaks(self, smoke_node):
        state = _tg_bot_state()

        with _TgBotEnv(getme=_getme_response(), ssh_error=Exception("API down")):
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "SSH" in check["detail"]


class TestSmokeTesterTgBotMissingToken:
    """No bot token in state — fail loudly instead of skipping the check."""

    async def test_fail_without_token(self, smoke_node):
        state = _tg_bot_state(secret_values={})

        with _TgBotEnv(getme=_getme_response(), ps_stdout="tg_bot\n"):
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "fail"
        assert "TELEGRAM_BOT_TOKEN" in check["detail"]


class TestSmokeTesterTgBotNoResource:
    """The bot module has no allocation — nothing was verified, so smoke fails."""

    async def test_fail_without_allocation(self, smoke_node):
        state = _make_state(modules=["tg_bot"], allocated_resources={})

        result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["module"] == "tg_bot"
        assert check["result"] == "fail"
        assert "cannot be verified" in check["detail"]


class TestSmokeTesterTgBotMissingServerHandle:
    """No server handle for the bot — fail loudly instead of skipping."""

    async def test_fail_without_server_handle(self, smoke_node):
        state = _tg_bot_state(server_handle=None)

        with _TgBotEnv(getme=_getme_response(), ps_stdout="tg_bot\n"):
            result = await smoke_node.run(state)

        assert result["smoke_result"]["status"] == "fail"
        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "server_handle" in check["detail"]


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
        "project_spec": {
            "title": "My Cool Project",
            "slug": "my-cool-project-0000",
            "config": {"modules": modules},
        },
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
        "secret_values": {},
        "non_secret_values": {},
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
            mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
            mock_asyncssh.import_private_key = MagicMock(return_value="parsed-key")
            mock_asyncssh.connect = MagicMock(return_value=mock_conn)

            result = await smoke_node.run(state)

        check = result["smoke_result"]["checks"][0]
        assert check["result"] == "fail"
        assert "ModuleNotFoundError" in check["detail"]
        assert "HTTP 500" in check["detail"]
        log_cmd = mock_conn.run.await_args.args[0]
        assert "cd /opt/services/my-cool-project-0000" in log_cmd
        assert "docker compose -p my-cool-project-0000" in log_cmd

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

            mock_api.get_server = AsyncMock(return_value=MagicMock(ssh_user="dev"))
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
