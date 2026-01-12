"""Tests for GitHub secrets encryption functionality."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.clients.github import GitHubAppClient


class TestSetRepositorySecret:
    """Tests for GitHubAppClient.set_repository_secret."""

    @pytest.fixture
    def github_client(self):
        """Create a GitHubAppClient with mocked credentials."""
        with patch.dict("os.environ", {"GITHUB_APP_ID": "12345"}):
            client = GitHubAppClient()
            client._private_key = "fake-private-key"
            return client

    @pytest.fixture
    def mock_public_key(self):
        """Generate a valid NaCl public key for testing."""
        from nacl import public

        keypair = public.PrivateKey.generate()
        return base64.b64encode(bytes(keypair.public_key)).decode()

    @pytest.mark.asyncio
    async def test_set_repository_secret_encrypts_and_sends(self, github_client, mock_public_key):
        """Test that set_repository_secret encrypts value and calls GitHub API."""
        with (
            patch.object(github_client, "get_token", new_callable=AsyncMock) as mock_get_token,
            patch("httpx.AsyncClient") as mock_client_class,
        ):
            mock_get_token.return_value = "fake-token"

            # Mock HTTP responses
            mock_client = AsyncMock()
            mock_client_class.return_value.__aenter__.return_value = mock_client

            # Mock public key response
            public_key_response = MagicMock()
            public_key_response.raise_for_status = MagicMock()
            public_key_response.json.return_value = {
                "key": mock_public_key,
                "key_id": "key-123",
            }

            # Mock secret creation response
            create_secret_response = MagicMock()
            create_secret_response.raise_for_status = MagicMock()

            mock_client.request.side_effect = [public_key_response, create_secret_response]

            # Call the method
            await github_client.set_repository_secret(
                owner="test-org",
                repo="test-repo",
                secret_name="MY_SECRET",  # noqa: S106
                secret_value="super-secret-value",  # noqa: S106
            )

            # Verify get_token was called
            mock_get_token.assert_called_once_with("test-org", "test-repo")

            # Verify request calls
            assert mock_client.request.call_count == 2  # noqa: PLR2004

            # 1. Verification of Public Key Fetch
            # call_args_list[0] -> ("GET", url), {headers=...}
            first_call = mock_client.request.call_args_list[0]
            assert first_call[0][0] == "GET"
            assert "/actions/secrets/public-key" in first_call[0][1]

            # 2. Verification of Secret Creation
            second_call = mock_client.request.call_args_list[1]
            assert second_call[0][0] == "PUT"
            assert "/actions/secrets/MY_SECRET" in second_call[0][1]

            # Verify encrypted value was sent
            json_payload = second_call[1]["json"]
            assert "encrypted_value" in json_payload
            assert json_payload["key_id"] == "key-123"
            # Encrypted value should be base64 encoded
            assert len(json_payload["encrypted_value"]) > 0

    @pytest.mark.asyncio
    async def test_set_repository_secrets_batch(self, github_client, mock_public_key):
        """Test that set_repository_secrets sets multiple secrets."""
        with (
            patch.object(
                github_client,
                "set_repository_secret",
                new_callable=AsyncMock,
            ) as mock_set_secret,
        ):
            secrets = {
                "SECRET_1": "value1",
                "SECRET_2": "value2",
                "SECRET_3": "value3",
            }

            count = await github_client.set_repository_secrets(
                owner="test-org",
                repo="test-repo",
                secrets=secrets,
            )

            assert count == 3  # noqa: PLR2004
            assert mock_set_secret.call_count == 3  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_set_repository_secrets_partial_failure(self, github_client, mock_public_key):
        """Test that set_repository_secrets continues after failure."""
        call_count = 0

        async def mock_set_secret(owner, repo, secret_name, secret_value):
            nonlocal call_count
            call_count += 1
            if secret_name == "SECRET_2":  # noqa: S105
                raise Exception("API error")

        with patch.object(
            github_client,
            "set_repository_secret",
            side_effect=mock_set_secret,
        ):
            secrets = {
                "SECRET_1": "value1",
                "SECRET_2": "value2",
                "SECRET_3": "value3",
            }

            count = await github_client.set_repository_secrets(
                owner="test-org",
                repo="test-repo",
                secrets=secrets,
            )

            # Only 2 succeeded
            assert count == 2  # noqa: PLR2004
            # All 3 were attempted
            assert call_count == 3  # noqa: PLR2004
