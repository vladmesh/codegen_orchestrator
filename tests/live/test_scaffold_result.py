"""Steps 7-8: Scaffold result verification — Issues #1 and #2.

Runs a real scaffold through the orchestrator pipeline:
  API (project + repo) → scaffold:queue → scaffolder → GitHub

Then checks the generated repo for:
  - Issue #1: pre-push hook should fail (not exit 0) when no lint tools available
  - Issue #2: .env.test should contain a valid-format TELEGRAM_BOT_TOKEN
  - Bonus: registry secrets should be set on the repo

These tests ARE expected to fail because the issues are in service-template.
After fixing service-template and re-scaffolding, they should pass.
"""

import asyncio
from pathlib import Path
import re
import secrets
import uuid

from live_harness import OwnershipManifest, cleanup_guard, cleanup_on_error, resolve_repo_root
from pipeline_helpers import cleanup_all
import pytest

from shared.contracts.dto.project import ProjectStatus
from shared.queues import SCAFFOLD_QUEUE

GITHUB_ORG = "project-factory-organization"
TEMPLATE_REPO = "gh:vladmesh/service-template"

SCAFFOLD_TIMEOUT = 120  # seconds
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))


@pytest.fixture
async def scaffolded_project(api, api_internal, compose_exec):
    """Create project + repo, scaffold it, yield (project, repo_name), cleanup."""
    suffix = secrets.token_hex(4)
    project_name = f"live-test-{suffix}"
    project_id = str(uuid.uuid4())
    repo_name = project_name

    # 1. Create project
    resp = await api.post(
        "/api/projects/",
        json={
            "id": project_id,
            "name": project_name,
            "status": ProjectStatus.DRAFT,
            "config": {"description": "live test scaffold"},
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201, f"Create project failed: {resp.text}"
    manifest = OwnershipManifest(project_id)
    manifest.own("project", project_id)
    ctx = {
        "project_id": project_id,
        "project_name": project_name,
        "repo_name": repo_name,
        "manifest": manifest,
    }

    # 2. Create repository record
    async with cleanup_on_error(lambda: cleanup_all(api_internal, None, ctx)):
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")
        resp = await api.post(
            "/api/repositories/",
            json={
                "project_id": project_id,
                "name": repo_name,
                "git_url": f"https://github.com/{GITHUB_ORG}/{repo_name}",
            },
        )
        resp.raise_for_status()
        assert resp.status_code == 201, f"Create repository failed: {resp.text}"
        repo_id = resp.json()["id"]

    async with cleanup_guard(
        lambda: cleanup_all(api_internal, None, ctx), manifest=ctx["manifest"]
    ):
        # 3. Publish ScaffoldMessage to scaffold:queue via redis
        manifest.own("github_repository", f"{GITHUB_ORG}/{repo_name}")
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")
        scaffold_msg = {
            "project_id": project_id,
            "repository_id": repo_id,
            "user_id": "live-test",
            "template_repo": TEMPLATE_REPO,
            "project_name": project_name,
            "modules": "backend,tg_bot",
            "task_description": "Live test scaffold",
        }

        import subprocess

        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "XADD",
                SCAFFOLD_QUEUE,
                "*",
                *[item for pair in scaffold_msg.items() for item in pair],
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=ORCHESTRATOR_ROOT,
        )
        assert result.returncode == 0, f"XADD failed: {result.stderr}"
        manifest.own("redis_entry", result.stdout.strip(), stream=SCAFFOLD_QUEUE)
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")

        # 4. Wait for scaffold to complete
        # After ProjectStatus split (#22), scaffold success sets status to 'active'.
        # Failure leaves status as 'draft' — we detect via timeout.
        for _ in range(SCAFFOLD_TIMEOUT // 2):
            await asyncio.sleep(2)
            resp = await api.get(f"/api/projects/{project_id}")
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == ProjectStatus.ACTIVE:
                break
        else:
            pytest.fail(
                f"Scaffold timed out ({SCAFFOLD_TIMEOUT}s) for {project_name}, status={status}"
            )

        yield ctx


def _get_file_from_github(compose_exec, repo_name: str, path: str) -> str | None:
    """Read a file from the scaffolded GitHub repo via the langgraph container."""
    script = f"""
import asyncio, sys, json
sys.path.insert(0, '/app')
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    content = await gh.get_file_contents('{GITHUB_ORG}', '{repo_name}', '{path}')
    if content is None:
        print('__FILE_NOT_FOUND__')
    else:
        print(content)
asyncio.run(main())
"""
    import subprocess

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "langgraph", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if output == "__FILE_NOT_FOUND__":
        return None
    return output


def _list_github_secrets(compose_exec, repo_name: str) -> list[str]:
    """List secret names on the GitHub repo via GitHubAppClient."""
    script = f"""
import asyncio, sys
sys.path.insert(0, '/app')
from shared.clients.github import GitHubAppClient
async def main():
    gh = GitHubAppClient()
    token = await gh.get_org_token('{GITHUB_ORG}')
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            'https://api.github.com/repos/{GITHUB_ORG}/{repo_name}/actions/secrets',
            headers={{
                'Authorization': f'token {{token}}',
                'Accept': 'application/vnd.github+json',
            }},
        )
        if resp.status_code == 200:
            for s in resp.json().get('secrets', []):
                print(s['name'])
        else:
            print(f'ERROR:{{resp.status_code}}:{{resp.text[:200]}}')
asyncio.run(main())
"""
    import subprocess

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "langgraph", "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        return []
    lines = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    # Filter out log lines from structlog
    return [line for line in lines if not line.startswith(("2026-", "{", "["))]


class TestScaffoldPrePushHook:
    """Issue #1: pre-push hook silently skips checks when no lint tools available."""

    def test_hook_does_not_exit_zero_without_tools(self, compose_exec, scaffolded_project):
        """Pre-push hook should fail (not silently pass) when no lint tools found.

        RED: service-template hook has `exit 0` fallback when neither Docker nor ruff available.
        """
        hook = _get_file_from_github(
            compose_exec, scaffolded_project["repo_name"], ".githooks/pre-push"
        )
        assert hook is not None, ".githooks/pre-push not found in scaffolded repo"

        # The hook should NOT silently exit 0 in the native fallback
        # when Docker is not available and ruff is not in PATH.
        # Find the else block after the ruff check — it must exit 1, not 0.
        lines = hook.splitlines()
        for i, line in enumerate(lines):
            if "neither docker nor ruff" in line.lower():
                # Look at nearby lines for exit code
                nearby = "\n".join(lines[max(0, i - 2) : i + 5])
                assert "exit 1" in nearby, (
                    "Pre-push hook silently exits 0 when no lint tools available. "
                    "This lets agent push code with lint errors, causing CI failures.\n"
                    f"Block:\n{nearby}"
                )
                break
        else:
            # If the warning line is gone, that's also acceptable (hook refactored)
            pass


class TestScaffoldEnvTest:
    """Issue #2: .env.test missing TELEGRAM_BOT_TOKEN with valid format."""

    def test_env_test_has_valid_bot_token(self, compose_exec, scaffolded_project):
        """Scaffolded .env.test should have TELEGRAM_BOT_TOKEN in valid aiogram format.

        RED: service-template .env.test.jinja doesn't include TELEGRAM_BOT_TOKEN.
        """
        env_test = _get_file_from_github(
            compose_exec, scaffolded_project["repo_name"], "infra/.env.test"
        )
        assert env_test is not None, ".env.test not found in scaffolded repo"
        assert "TELEGRAM_BOT_TOKEN" in env_test, (
            ".env.test does not contain TELEGRAM_BOT_TOKEN. "
            "Integration tests will crash with aiogram TokenValidationError."
        )

        # Token must match aiogram's expected format: digits:alphanumeric
        token_match = re.search(r"TELEGRAM_BOT_TOKEN=(\S+)", env_test)
        assert token_match, "TELEGRAM_BOT_TOKEN has no value"
        token_value = token_match.group(1)
        assert re.match(r"\d+:[A-Za-z0-9_-]+", token_value), (
            f"TELEGRAM_BOT_TOKEN={token_value} is not in valid format (digits:alphanum). "
            "aiogram validates token format and will crash."
        )


class TestScaffoldRegistrySecrets:
    """Issue #3: Registry secrets should be set on the GitHub repo after scaffold."""

    def test_registry_secrets_exist_on_repo(self, compose_exec, scaffolded_project):
        """After scaffold, the GitHub repo should have REGISTRY_* secrets.

        RED: scaffolder fails with 'No module named nacl' — pynacl not installed.
        GitHub Secrets API requires PyNaCl for encrypting secret values.
        """
        secret_names = _list_github_secrets(compose_exec, scaffolded_project["repo_name"])
        assert "REGISTRY_URL" in secret_names, (
            "REGISTRY_URL secret not set on repo. "
            "Check scaffolder logs for 'No module named nacl' — pynacl missing."
        )
        assert "REGISTRY_USER" in secret_names, "REGISTRY_USER secret not set on repo"
        assert "REGISTRY_PASSWORD" in secret_names, "REGISTRY_PASSWORD secret not set on repo"
