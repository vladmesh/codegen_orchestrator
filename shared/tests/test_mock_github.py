import httpx
import pytest

from shared.tests.mocks.github import MockGitHubClient


@pytest.mark.asyncio
class TestMockGitHubClient:
    async def test_create_and_get_repo(self):
        client = MockGitHubClient()
        repo = await client.create_repo(org="test-org", name="test-repo")

        assert repo.name == "test-repo"
        assert repo.full_name == "test-org/test-repo"
        assert repo.html_url == "https://github.com/test-org/test-repo"

        fetched = await client.get_repo("test-org", "test-repo")
        assert fetched.id == repo.id

    async def test_get_non_existent_repo_raises_error(self):
        client = MockGitHubClient()
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await client.get_repo("test-org", "does-not-exist")
        assert exc.value.response.status_code == httpx.codes.NOT_FOUND

    async def test_file_operations(self):
        client = MockGitHubClient()
        await client.create_repo("org", "repo")

        # Create file
        await client.create_or_update_file("org", "repo", "README.md", "Hello World", "init")

        # Read file
        content = await client.get_file_contents("org", "repo", "README.md")
        assert content == "Hello World"

        # Update file
        await client.create_or_update_file("org", "repo", "README.md", "Hello Updated", "update")
        content = await client.get_file_contents("org", "repo", "README.md")
        assert content == "Hello Updated"

    async def test_secrets_management(self):
        client = MockGitHubClient()
        await client.create_repo("org", "repo")

        await client.set_repository_secret("org", "repo", "API_KEY", "secret123")

        # Verify secret stored in mock state
        assert client.secrets["repo"]["API_KEY"] == "secret123"

        # Bulk set
        await client.set_repository_secrets("org", "repo", {"DB_URL": "postgres://..."})
        assert client.secrets["repo"]["DB_URL"] == "postgres://..."

    async def test_provision_project_repo_success(self):
        client = MockGitHubClient()

        repo = await client.provision_project_repo(
            name="My Project", project_spec={"name": "test"}, secrets={"TOKEN": "xyz"}
        )

        # Check name sanitization
        assert repo.name == "my-project"

        # Check file created
        config = await client.get_file_contents("mock-org", "my-project", ".project.yaml")
        assert "name: test" in config

        # Check secret set
        assert client.secrets["my-project"]["TOKEN"] == "xyz"  # noqa: S105

    async def test_provision_project_idempotency(self):
        client = MockGitHubClient()

        # First provision
        repo1 = await client.provision_project_repo(name="test-repo")

        # Second provision should return same repo without error
        repo2 = await client.provision_project_repo(name="test-repo")

        assert repo1.id == repo2.id
