"""Pipeline test: Scaffold phase only (~1-2 min).

Exercises: project creation → scaffold:queue → scaffolder → GitHub repo.
Verifies the scaffolded project exists and has the expected structure.
"""

import httpx
from pipeline_helpers import (
    API_URL,
    AUTH_HEADERS,
    GITHUB_ORG,
    SCAFFOLD_TIMEOUT,
    cleanup_all,
    create_noop_project,
    docker_exec,
    dump_debug,
    ensure_test_user,
    trigger_scaffold,
    wait_scaffold,
)
import pytest
import pytest_asyncio

from shared.contracts.dto.project import ProjectStatus

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def scaffold_ctx():
    """Scaffold pipeline: create project + repo, trigger scaffold, wait."""
    async with httpx.AsyncClient(base_url=API_URL, timeout=10, headers=AUTH_HEADERS) as api:
        await ensure_test_user(api)
        ctx = await create_noop_project(api)
        trigger_scaffold(ctx)
        await wait_scaffold(api, ctx, timeout=SCAFFOLD_TIMEOUT)

        yield ctx

        if ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
            dump_debug(ctx, "scaffold")
        await cleanup_all(api, None, ctx)


class TestScaffoldPipeline:
    """Scaffold pipeline: project → scaffold:queue → scaffolded."""

    async def test_project_scaffolded(self, scaffold_ctx):
        """Project status transitions to 'scaffolded'."""
        assert scaffold_ctx["scaffold_status"] == ProjectStatus.ACTIVE, (
            f"Scaffold failed — status: {scaffold_ctx.get('scaffold_status')}"
        )

    async def test_github_repo_has_ci(self, scaffold_ctx):
        """Scaffolded repo has .github/workflows/ci.yml."""
        if scaffold_ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
            pytest.skip("scaffold failed — cannot check repo")

        repo_name = scaffold_ctx["repo_name"]
        script = (
            "import asyncio, sys\n"
            "sys.path.insert(0, '/app')\n"
            "from shared.clients.github import GitHubAppClient\n"
            "async def main():\n"
            "    gh = GitHubAppClient()\n"
            f"    content = await gh.get_file_contents('{GITHUB_ORG}', '{repo_name}', "
            "'.github/workflows/ci.yml')\n"
            "    print('FOUND' if content else 'MISSING')\n"
            "asyncio.run(main())\n"
        )
        result = docker_exec("langgraph", script, timeout=15)
        assert "FOUND" in result.stdout, (
            f"ci.yml not found in {GITHUB_ORG}/{repo_name}. "
            f"stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}"
        )

    async def test_github_repo_has_makefile(self, scaffold_ctx):
        """Scaffolded repo has a Makefile."""
        if scaffold_ctx.get("scaffold_status") != ProjectStatus.ACTIVE:
            pytest.skip("scaffold failed — cannot check repo")

        repo_name = scaffold_ctx["repo_name"]
        script = (
            "import asyncio, sys\n"
            "sys.path.insert(0, '/app')\n"
            "from shared.clients.github import GitHubAppClient\n"
            "async def main():\n"
            "    gh = GitHubAppClient()\n"
            f"    content = await gh.get_file_contents('{GITHUB_ORG}', '{repo_name}', "
            "'Makefile')\n"
            "    print('FOUND' if content else 'MISSING')\n"
            "asyncio.run(main())\n"
        )
        result = docker_exec("langgraph", script, timeout=15)
        assert "FOUND" in result.stdout, (
            f"Makefile not found in {GITHUB_ORG}/{repo_name}. "
            f"stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}"
        )
