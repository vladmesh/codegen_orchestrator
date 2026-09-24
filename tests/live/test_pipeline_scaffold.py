"""Pipeline test: Scaffold phase only (~1-2 min).

Exercises: project creation → scaffold:queue → scaffolder → GitHub repo.
Verifies the scaffolded project exists and has the expected structure.
"""

from live_harness import cleanup_guard
from pipeline_helpers import (
    GITHUB_ORG,
    ScaffoldDidNotComplete,
    api_client_as_internal_service,
    api_client_as_test_user,
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
    async with (
        api_client_as_test_user() as api,
        api_client_as_internal_service() as api_internal,
    ):
        await ensure_test_user(api, api_internal)
        ctx = await create_noop_project(api, api_internal)
        async with cleanup_guard(
            lambda: cleanup_all(api_internal, None, ctx), manifest=ctx["manifest"]
        ):
            trigger_scaffold(ctx)
            try:
                await wait_scaffold(api, ctx)
            except ScaffoldDidNotComplete:
                dump_debug(ctx, "scaffold")
                raise

            yield ctx


class TestScaffoldPipeline:
    """Scaffold pipeline: project → scaffold:queue → scaffolded."""

    async def test_project_scaffolded(self, scaffold_ctx):
        """Project status transitions to 'scaffolded'."""
        assert scaffold_ctx["scaffold_status"] == ProjectStatus.ACTIVE, (
            f"Scaffold failed — status: {scaffold_ctx.get('scaffold_status')}"
        )

    async def test_github_repo_has_ci(self, scaffold_ctx):
        """Scaffolded repo has .github/workflows/ci.yml."""
        assert scaffold_ctx.get("scaffold_status") == ProjectStatus.ACTIVE, (
            "the scaffold phase failed, so there is no repository to check: status "
            f"{scaffold_ctx.get('scaffold_status')}"
        )

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
        assert scaffold_ctx.get("scaffold_status") == ProjectStatus.ACTIVE, (
            "the scaffold phase failed, so there is no repository to check: status "
            f"{scaffold_ctx.get('scaffold_status')}"
        )

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
