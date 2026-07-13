"""Unit tests for SSH key persistence after provisioning."""

from unittest.mock import AsyncMock, patch

import pytest

from src.provisioner.api_client import save_server_ssh_key
from src.provisioner.ssh_manager import SSHManager


class TestSSHManagerGetPrivateKey:
    """Test SSHManager.get_private_key() method."""

    def test_reads_existing_key(self, tmp_path):
        """Returns private key content from disk."""
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("fake-private-key-content")

        manager = SSHManager(key_path=str(key_path))
        assert manager.get_private_key() == "fake-private-key-content"

    def test_returns_none_when_no_key(self, tmp_path):
        """Returns None when key file does not exist."""
        manager = SSHManager(key_path=str(tmp_path / "nonexistent"))
        assert manager.get_private_key() is None


class TestSaveServerSSHKey:
    """Test save_server_ssh_key api_client function."""

    @pytest.mark.asyncio
    async def test_saves_key_via_api(self):
        """Calls api_client.update_server with ssh_key field."""
        with patch("src.provisioner.api_client.api_client") as mock_api:
            mock_api.update_server = AsyncMock()

            result = await save_server_ssh_key("srv-1", "my-private-key")

            assert result is None
            mock_api.update_server.assert_called_once_with("srv-1", {"ssh_key": "my-private-key"})

    @pytest.mark.asyncio
    async def test_propagates_api_error(self):
        """An API failure is not converted into a false success signal."""
        with patch("src.provisioner.api_client.api_client") as mock_api:
            mock_api.update_server = AsyncMock(side_effect=RuntimeError("API down"))

            with pytest.raises(RuntimeError, match="API down"):
                await save_server_ssh_key("srv-1", "my-private-key")
