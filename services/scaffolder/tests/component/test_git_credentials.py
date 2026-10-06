"""Scaffolder's native auth leaves a clean workspace for worker preparation."""

import base64
import os
from types import SimpleNamespace

import pytest

from shared.tests.git_http_fixture import GitHTTPFixture
from src.scaffold import run_ensure_workspace


@pytest.mark.subprocess
@pytest.mark.asyncio
async def test_ensure_workspace_clones_with_native_auth_and_clean_config(tmp_path, monkeypatch):
    token = "scaffolder-component-harmless-canary"  # noqa: S105 - harmless canary
    with GitHTTPFixture(tmp_path, token) as remote:
        for key, value in remote.environment.items():
            monkeypatch.setenv(key, value)
        root = tmp_path / "workspaces"
        result = await run_ensure_workspace(
            repository_id="repo-id",
            project_name="fixture",
            repo_full_name="org/repo",
            github_token=token,
            settings=SimpleNamespace(workspace_base_path=str(root)),
            repo_exists_on_github=True,
        )
        assert result.success, result.error
        workspace = root / "repo-id"
        config = (workspace / ".git" / "config").read_text()
        assert "https://github.com/org/repo" in config
        assert "extraheader" not in config.lower()
        assert "helper" not in config.lower()
        assert token not in config + str(remote.argv()) + str(result)
        assert base64.b64encode(f"x-access-token:{token}".encode()).decode() not in config
        assert remote.headers
        # An ensure of this populated workspace must leave its clean auth state intact.
        again = await run_ensure_workspace(
            repository_id="repo-id",
            project_name="fixture",
            repo_full_name="org/repo",
            github_token=token,
            settings=SimpleNamespace(workspace_base_path=str(root)),
            repo_exists_on_github=True,
        )
        assert again.success and again.skipped
        assert (workspace / ".git" / "config").read_text() == config
        assert "GIT_CONFIG_VALUE_0" not in os.environ
