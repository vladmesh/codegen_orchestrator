"""Unit tests for PO tools encryption integration."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSetProjectSecretEncryption:
    """Tests for set_project_secret encryption."""

    @pytest.mark.asyncio
    @patch("src.po.tools.encrypt_dict")
    @patch("src.po.tools.decrypt_dict")
    async def test_encrypts_secret_before_saving(self, mock_decrypt, mock_encrypt):
        """set_project_secret should decrypt existing, add new, then encrypt before PATCH."""
        mock_decrypt.return_value = {"OLD_KEY": "old-value"}
        mock_encrypt.return_value = {
            "OLD_KEY": "gAAAAA-old-encrypted",
            "NEW_KEY": "gAAAAA-new-encrypted",
        }

        # Mock the API client
        mock_response_get = MagicMock()
        mock_response_get.json.return_value = {
            "config": {"secrets": {"OLD_KEY": "gAAAAA-old-encrypted"}}
        }
        mock_response_get.raise_for_status = MagicMock()

        mock_response_patch = MagicMock()
        mock_response_patch.raise_for_status = MagicMock()

        mock_api = AsyncMock()
        mock_api.get = AsyncMock(return_value=mock_response_get)
        mock_api.patch = AsyncMock(return_value=mock_response_patch)

        with patch("src.po.tools._get_api", return_value=mock_api):
            from src.po.tools import set_project_secret

            result = await set_project_secret.ainvoke(
                {"project_id": "proj-1", "key": "NEW_KEY", "value": "new-value"},
                config={"configurable": {"thread_id": "po-user-1", "user_id": "1"}},
            )

        mock_decrypt.assert_called_once_with({"OLD_KEY": "gAAAAA-old-encrypted"})
        mock_encrypt.assert_called_once_with({"OLD_KEY": "old-value", "NEW_KEY": "new-value"})

        # Verify PATCH was called with encrypted values
        patch_call = mock_api.patch.call_args
        saved_config = patch_call[1]["json"]["config"]
        assert saved_config["secrets"] == {
            "OLD_KEY": "gAAAAA-old-encrypted",
            "NEW_KEY": "gAAAAA-new-encrypted",
        }
        assert "Secret 'NEW_KEY' set" in result
