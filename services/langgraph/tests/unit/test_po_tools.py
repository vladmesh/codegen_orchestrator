"""Unit tests for PO tools — set_project_secret uses atomic merge endpoint."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSetProjectSecretAtomicMerge:
    """set_project_secret delegates to POST /config/secrets (no client-side crypto)."""

    @pytest.mark.asyncio
    async def test_posts_to_merge_endpoint(self):
        """set_project_secret should POST to /config/secrets, not GET+PATCH."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"keys": ["NEW_KEY"]}
        mock_response.raise_for_status = MagicMock()

        mock_api = AsyncMock()
        mock_api.post_raw = AsyncMock(return_value=mock_response)

        with patch("src.agents.po.tools_projects._get_api", return_value=mock_api):
            from src.agents.po.tools import set_project_secret

            result = await set_project_secret.ainvoke(
                {"project_id": "proj-1", "key": "NEW_KEY", "value": "new-value"},
                config={"configurable": {"thread_id": "po-chat-1", "telegram_chat_id": "1"}},
            )

        mock_api.post_raw.assert_called_once()
        call_args = mock_api.post_raw.call_args
        assert "projects/proj-1/config/secrets" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["secrets"] == {"NEW_KEY": "new-value"}
        assert "env_hints" not in payload
        assert "Secret 'NEW_KEY' set" in result

        # No GET or PATCH calls
        mock_api.get_raw.assert_not_called()
        mock_api.patch_raw.assert_not_called()
