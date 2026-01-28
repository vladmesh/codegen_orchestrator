"""Infrastructure Sanity Check - Phase 4 of E2E Engineering Test Plan.

This test validates that the core infrastructure works without any LLM involvement:
- GitHub App authentication
- Repository creation via API
- Copier scaffolding (service-template)
- Git clone/commit/push
- File verification via GitHub API

Prerequisites:
- GITHUB_APP_ID env var set
- GITHUB_APP_PRIVATE_KEY_PATH env var set (or GITHUB_PRIVATE_KEY_CONTENT)
- GitHub App installed on target org with repo admin permissions
"""

import os
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

import pytest
import pytest_asyncio

from shared.clients.github import GitHubAppClient

# Marker for tests requiring real GitHub credentials
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_github,
]

# Skip entire module if GitHub credentials not configured
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_KEY_PATH = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")
GITHUB_APP_KEY_CONTENT = os.getenv("GITHUB_PRIVATE_KEY_CONTENT")

# Test-specific org (from .env.test)
GITHUB_TEST_ORG = os.getenv("GITHUB_TEST_ORG")

# Set E2E_KEEP_REPOS=true to keep test repos for debugging
KEEP_REPOS = os.getenv("E2E_KEEP_REPOS", "").lower() in ("true", "1", "yes")

SKIP_REASON = "GitHub App credentials not configured (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH)"
GITHUB_CONFIGURED = GITHUB_APP_ID and (GITHUB_APP_KEY_PATH or GITHUB_APP_KEY_CONTENT)

# Template for scaffolding
SERVICE_TEMPLATE = os.getenv("SERVICE_TEMPLATE_PATH", "gh:vladmesh/service-template")


def run_copier(
    template: str,
    output_dir: Path,
    project_name: str,
    modules: str = "backend",
) -> subprocess.CompletedProcess:
    """Run copier to scaffold a project.

    Args:
        template: Path or URL to the copier template
        output_dir: Directory to output the generated project
        project_name: Name for the project
        modules: Comma-separated list of modules to include

    Returns:
        CompletedProcess from subprocess.run
    """
    data = {
        "project_name": project_name,
        "modules": modules,
        "project_description": "E2E infrastructure sanity test",
        "author_name": "E2E Test",
        "author_email": "e2e@test.local",
    }

    cmd = [
        "copier",
        "copy",
        "--trust",
        "--defaults",
        "--overwrite",  # Overwrite existing files (e.g., README.md from GitHub auto_init)
        "--vcs-ref=HEAD",
    ]

    for key, value in data.items():
        cmd.extend(["--data", f"{key}={value}"])

    cmd.extend([template, str(output_dir)])

    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command.

    Args:
        args: Git command arguments (without 'git' prefix)
        cwd: Working directory
        check: Whether to raise on non-zero exit code

    Returns:
        CompletedProcess from subprocess.run
    """
    return subprocess.run(
        ["git", *args],  # noqa: S607, S603
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest_asyncio.fixture
async def github_client():
    """Create a real GitHubAppClient for testing.

    Skips if credentials are not configured.
    """
    if not GITHUB_CONFIGURED:
        pytest.skip(SKIP_REASON)

    client = GitHubAppClient()
    yield client


@pytest_asyncio.fixture
async def cleanup_repos(github_client: GitHubAppClient):
    """Fixture that tracks repos to delete after test.

    Usage:
        cleanup_repos.append(("org", "repo-name"))

    Repos are deleted in reverse order after test completes.
    Set E2E_KEEP_REPOS=true to skip cleanup for debugging.
    """
    repos_to_delete: list[tuple[str, str]] = []

    yield repos_to_delete

    # Cleanup: delete all tracked repos (unless KEEP_REPOS is set)
    if KEEP_REPOS:
        print("\n[E2E_KEEP_REPOS=true] Keeping repos for debugging:")
        for org, repo in repos_to_delete:
            print(f"  - https://github.com/{org}/{repo}")
        return

    for org, repo in reversed(repos_to_delete):
        try:
            deleted = await github_client.delete_repo(org, repo)
            if deleted:
                print(f"Cleaned up repo: {org}/{repo}")
            else:
                print(f"Repo not found (already deleted?): {org}/{repo}")
        except Exception as e:
            print(f"Failed to cleanup repo {org}/{repo}: {e}")


@pytest_asyncio.fixture
async def test_org(github_client: GitHubAppClient) -> str:
    """Get the organization to use for test repos.

    Uses GITHUB_TEST_ORG if set, otherwise auto-detects from GitHub App installation.
    """
    if GITHUB_TEST_ORG:
        return GITHUB_TEST_ORG
    installation = await github_client.get_first_org_installation()
    return installation["org"]


class TestInfrastructureSanity:
    """Validates GitHub + Copier + Git infrastructure without LLM."""

    @pytest.mark.asyncio
    async def test_scaffold_and_push_to_real_github(
        self,
        github_client: GitHubAppClient,
        cleanup_repos: list[tuple[str, str]],
        test_org: str,
    ):
        """
        Full infrastructure test:
        1. Create repo on GitHub
        2. Clone to temp directory
        3. Run copier (backend module)
        4. git add + commit + push
        5. Verify file exists via GitHub API
        """
        # Generate unique repo name
        repo_name = f"e2e-sanity-{uuid4().hex[:8]}"
        cleanup_repos.append((test_org, repo_name))

        # 1. Create repo on GitHub
        print(f"\n1. Creating repo: {test_org}/{repo_name}")
        repo = await github_client.create_repo(
            org=test_org,
            name=repo_name,
            description="E2E infrastructure sanity test - auto-delete",
            private=True,
        )
        assert repo.name == repo_name
        assert repo.full_name == f"{test_org}/{repo_name}"
        print(f"   Created: {repo.html_url}")

        # 2. Clone repo
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"

            # Get token for authenticated clone
            token = await github_client.get_org_token(test_org)
            clone_url = f"https://x-access-token:{token}@github.com/{test_org}/{repo_name}.git"

            print(f"2. Cloning to {repo_dir}")
            result = run_git(["clone", clone_url, str(repo_dir)], cwd=Path(tmpdir))
            assert result.returncode == 0, f"Clone failed: {result.stderr}"
            print("   Cloned successfully")

            # Configure git user for commits
            run_git(["config", "user.email", "e2e@test.local"], cwd=repo_dir)
            run_git(["config", "user.name", "E2E Test"], cwd=repo_dir)

            # 3. Run copier
            print(f"3. Running copier with template: {SERVICE_TEMPLATE}")
            copier_result = run_copier(
                template=SERVICE_TEMPLATE,
                output_dir=repo_dir,
                project_name=repo_name,
                modules="backend",
            )
            if copier_result.returncode != 0:
                print(f"   Copier stdout: {copier_result.stdout}")
                print(f"   Copier stderr: {copier_result.stderr}")
                pytest.fail(f"Copier failed with code {copier_result.returncode}")
            print("   Copier completed successfully")

            # Verify expected files exist locally
            assert (repo_dir / "Makefile").exists(), "Makefile not generated"
            assert (repo_dir / "services" / "backend").is_dir(), "Backend service not generated"
            print("   Verified local files: Makefile, services/backend/")

            # Disable hooks (service-template includes hooks that require make)
            run_git(["config", "core.hooksPath", "/dev/null"], cwd=repo_dir)

            # 4. Commit and push
            print("4. Committing and pushing changes")
            run_git(["add", "."], cwd=repo_dir)

            # Check if there are changes to commit
            status_result = run_git(["status", "--porcelain"], cwd=repo_dir)
            status_str = status_result.stdout[:500] if status_result.stdout else "(empty)"
            print(f"   Git status: {status_str}")
            if not status_result.stdout.strip():
                pytest.fail("No changes to commit after copier")

            commit_result = run_git(
                ["commit", "-m", "chore: initial scaffold from service-template"],
                cwd=repo_dir,
                check=False,
            )
            if commit_result.returncode != 0:
                print(f"   Commit stderr: {commit_result.stderr}")
                print(f"   Commit stdout: {commit_result.stdout}")
                pytest.fail(f"Git commit failed: {commit_result.stderr}")
            push_result = run_git(["push", "origin", "main"], cwd=repo_dir, check=False)
            if push_result.returncode != 0:
                print(f"   Push stderr: {push_result.stderr}")
                print(f"   Push stdout: {push_result.stdout}")
                pytest.fail(f"Push failed: {push_result.stderr}")
            print("   Pushed to origin/main")

        # 5. Verify via GitHub API
        print("5. Verifying files via GitHub API")

        # Check Makefile exists
        makefile_content = await github_client.get_file_contents(test_org, repo_name, "Makefile")
        assert makefile_content is not None, "Makefile not found via GitHub API"
        assert len(makefile_content) > 0, "Makefile is empty"
        print("   Verified: Makefile exists and has content")

        # Check services/backend directory exists
        files = await github_client.list_repo_files(test_org, repo_name, "services")
        assert "backend" in files, f"Backend service not found. Files in services/: {files}"
        print("   Verified: services/backend/ exists")

        print("\n=== Infrastructure sanity check PASSED ===")

    @pytest.mark.asyncio
    async def test_github_client_authentication(self, github_client: GitHubAppClient):
        """Verify GitHub App authentication works."""
        # Just getting the installation proves auth works
        installation = await github_client.get_first_org_installation()
        assert installation["org"] is not None
        assert installation["installation_id"] is not None
        print(f"GitHub App authenticated for org: {installation['org']}")

    @pytest.mark.asyncio
    async def test_copier_runs_locally(self):
        """Verify copier CLI is available and can scaffold."""
        if not GITHUB_CONFIGURED:
            pytest.skip(SKIP_REASON)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_copier(
                template=SERVICE_TEMPLATE,
                output_dir=Path(tmpdir),
                project_name="copier-test",
                modules="backend",
            )
            assert result.returncode == 0, f"Copier failed: {result.stderr}"
            assert (Path(tmpdir) / "Makefile").exists()
            print("Copier CLI works and generates expected files")
