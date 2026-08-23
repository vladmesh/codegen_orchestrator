"""PO tools add_bot_user / remove_bot_user: typed audience mutations with a
truthful rollout report.

The tool must never say access changed live merely because the DB transaction
committed: applied, pending and failed come back as different texts, and the
audience itself is never reconstructed by the model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.clients.internal_api import InternalAPIClient
from src.agents.po import tools_projects
from src.agents.po.tools import (
    add_bot_user,
    init_po_clients,
    remove_bot_user,
)


@pytest.fixture(autouse=True)
def _init_clients(mock_api_client, mock_stream_client):
    init_po_clients(mock_api_client, mock_stream_client)


@pytest.fixture
def mock_api_client():
    return AsyncMock(spec=InternalAPIClient)


@pytest.fixture
def mock_stream_client():
    client = AsyncMock()
    client.redis = AsyncMock()
    return client


@pytest.fixture(autouse=True)
def _fast_polling():
    """Keep rollout polls tight so tests never really wait."""
    with (
        patch.object(tools_projects, "ROLLOUT_POLL_INTERVAL_SECONDS", 0.0),
        patch.object(tools_projects, "ROLLOUT_SYNC_WAIT_SECONDS", 0.2),
    ):
        yield


def _make_response(data, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json = MagicMock(return_value=data)
    if 200 <= status_code < 300:
        resp.raise_for_status = MagicMock(return_value=None)
    else:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=None, response=None)
        )
    return resp


def _make_config() -> dict:
    return {"configurable": {"thread_id": "po-chat-777", "telegram_chat_id": "777"}}


class TestAddBotUser:
    @pytest.mark.asyncio
    async def test_posts_the_typed_operation_and_reports_applied(self, mock_api_client):
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "only_me",
                "operation": "added",
                "audience": "42,84",
                "rollout": "pending",
                "rollout_run_id": "botrollout-1",
            }
        )
        mock_api_client.get_raw.return_value = _make_response({"rollout": "applied", "detail": ""})

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        call = mock_api_client.post_raw.call_args
        assert call.args[0] == "projects/abc/config/bot-access/users"
        assert call.kwargs["json"] == {"telegram_id": 84}
        assert "84" in result
        # Only an applied verdict may say the change reached the running bot.
        assert "live on the running bot" in result

    @pytest.mark.asyncio
    async def test_pending_rollout_never_claims_the_change_is_live(self, mock_api_client):
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "custom",
                "operation": "added",
                "audience": "42,84",
                "rollout": "pending",
                "rollout_run_id": "botrollout-2",
            }
        )
        # Every poll answers pending until the tool's own timeout fires.
        mock_api_client.get_raw.return_value = _make_response({"rollout": "pending", "detail": ""})

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        assert mock_api_client.get_raw.call_count >= 1
        assert "has not finished" in result or "still being applied" in result
        # The tool never claims the change is live when it is not.
        assert "live on the running bot" not in result

    @pytest.mark.asyncio
    async def test_timed_out_pending_defers_the_outcome_proactively(self, mock_api_client):
        """When the bounded wait ends pending, the ending is promised and the
        notify-owed marker is written so the sweep delivers it later."""
        mock_api_client.post_raw.side_effect = [
            _make_response(
                {
                    "mode": "custom",
                    "operation": "added",
                    "audience": "42,84",
                    "rollout": "pending",
                    "rollout_run_id": "botrollout-2b",
                }
            ),
            _make_response({"state": "owed"}),
        ]
        mock_api_client.get_raw.return_value = _make_response({"rollout": "pending", "detail": ""})

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        owed = mock_api_client.post_raw.call_args_list[1]
        assert owed.args[0] == "projects/abc/config/bot-access/rollouts/botrollout-2b/notify-owed"
        assert "message you here as soon as the rollout finishes" in result
        assert "live on the running bot" not in result

    @pytest.mark.asyncio
    async def test_transient_status_poll_errors_do_not_end_the_wait(self, mock_api_client):
        """An API blip during polling reads as still-pending, never as a verdict."""
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "custom",
                "operation": "added",
                "audience": "42,84",
                "rollout": "pending",
                "rollout_run_id": "botrollout-2c",
            }
        )
        responses = [
            httpx.ConnectError("boom"),
            _make_response({"rollout": "applied", "detail": ""}),
        ]

        async def flaky_get(*args, **kwargs):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        mock_api_client.get_raw.side_effect = flaky_get

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        assert mock_api_client.get_raw.call_count >= 2
        assert "live on the running bot" in result

    @pytest.mark.asyncio
    async def test_not_deployed_project_says_config_only(self, mock_api_client):
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "only_me",
                "operation": "already_present",
                "audience": "42",
                "rollout": "not_deployed",
                "rollout_run_id": None,
            }
        )

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 42}, config=_make_config()
        )

        assert "already" in result
        mock_api_client.get_raw.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_rollout_is_reported_as_failed(self, mock_api_client):
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "only_me",
                "operation": "added",
                "audience": "42,84",
                "rollout": "pending",
                "rollout_run_id": "botrollout-3",
            }
        )
        mock_api_client.get_raw.return_value = _make_response(
            {"rollout": "failed", "detail": "deploy workflow failed: smoke"}
        )

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        assert "FAILED" in result
        assert "smoke" in result
        # A failure report must not read like success.
        assert "live on the running bot" not in result

    @pytest.mark.asyncio
    async def test_not_deployed_mutation_reports_the_next_deploy(self, mock_api_client):
        mock_api_client.post_raw.return_value = _make_response(
            {
                "mode": "only_me",
                "operation": "added",
                "audience": "42,84",
                "rollout": "not_deployed",
                "rollout_run_id": None,
            }
        )

        result = await add_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        mock_api_client.get_raw.assert_not_called()
        assert "not deployed" in result.lower() or "next deploy" in result.lower()


class TestRemoveBotUser:
    @pytest.mark.asyncio
    async def test_delete_the_typed_operation_and_report_applied(self, mock_api_client):
        mock_api_client.delete_raw.return_value = _make_response(
            {
                "mode": "only_me",
                "operation": "removed",
                "audience": "42",
                "rollout": "pending",
                "rollout_run_id": "botrollout-5",
            }
        )
        mock_api_client.get_raw.return_value = _make_response({"rollout": "applied", "detail": ""})

        result = await remove_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 84}, config=_make_config()
        )

        call = mock_api_client.delete_raw.call_args
        assert call.args[0] == "projects/abc/config/bot-access/users/84"
        assert "removed" in result.lower()
        assert "live on the running bot" in result

    @pytest.mark.asyncio
    async def test_server_refusal_comes_back_as_an_error(self, mock_api_client):
        mock_api_client.delete_raw.return_value = _make_response(
            {"detail": "removing the final allowed ID would make the bot public"}, 422
        )

        result = await remove_bot_user.ainvoke(
            {"project_id": "abc", "telegram_id": 42}, config=_make_config()
        )

        assert result.startswith("Error:")
        mock_api_client.get_raw.assert_not_called()
