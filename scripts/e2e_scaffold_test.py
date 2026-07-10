"""E2E Scaffold Test.

Publishes a CreateWorkerCommand with ScaffoldConfig directly to worker:commands.
Worker-manager picks it up, creates container, runs copier + make setup + git push.

Verifies:
  1. CreateWorkerResponse(success=true) from worker-manager
  2. Scaffold pushed to GitHub (expected files exist in repo)
  3. Cleanup: deletes GitHub repo, prints worker_id for container cleanup

Run inside langgraph container:
  docker compose exec -T langgraph python < scripts/e2e_scaffold_test.py

Or via Makefile (handles container cleanup):
  make test-e2e-scaffold
"""

import asyncio
import json
import os
import sys
import uuid

import redis.asyncio as aioredis

# Ensure shared is importable
sys.path.insert(0, "/app")

from shared.clients.github import GitHubAppClient  # noqa: E402
from shared.contracts.queues.worker import (  # noqa: E402
    AgentType,
    CreateWorkerCommand,
    ScaffoldConfig,
    WorkerCapability,
    WorkerConfig,
)

GITHUB_ORG = os.getenv("GITHUB_ORG", "project-factory-organization")
TEST_REPO_NAME = "scaffold-e2e-test"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
COMMAND_STREAM = "worker:commands"
RESPONSE_STREAM = "worker:responses:developer"

# Files that must exist after scaffold with "backend" module
# .copier-answers.yml = copier ran; services = framework sync_services ran
EXPECTED_FILES = ["Makefile", "pyproject.toml", ".github", ".copier-answers.yml", "services"]


async def _create_repo(github):
    """Create GitHub repo (or skip if exists)."""
    print(f"[2] Creating repo {GITHUB_ORG}/{TEST_REPO_NAME} ...")
    try:
        repo = await github.create_repo(
            org=GITHUB_ORG,
            name=TEST_REPO_NAME,
            description="E2E scaffold test (auto-created)",
            private=True,
        )
        print(f"    Repo created: {repo.html_url}")
    except Exception as e:
        if "already exists" in str(e).lower() or "422" in str(e):
            print("    Repo already exists, continuing.")
        else:
            raise


async def _wait_for_response(r, group, consumer, request_id):
    """Wait for CreateWorkerResponse from worker-manager."""
    deadline = asyncio.get_running_loop().time() + 300
    while asyncio.get_running_loop().time() < deadline:
        msgs = await r.xreadgroup(group, consumer, {RESPONSE_STREAM: ">"}, count=1, block=2000)
        if msgs:
            for _, stream_msgs in msgs:
                for msg_id, msg_data in stream_msgs:
                    data = json.loads(msg_data.get("data", "{}"))
                    await r.xack(RESPONSE_STREAM, group, msg_id)
                    if data.get("request_id") == request_id:
                        return data
    return None


async def _verify_scaffold(github, repo_full):
    """Verify that scaffold pushed expected files to GitHub."""
    owner, repo = repo_full.split("/")
    print(f"[6] Verifying scaffold results in {repo_full} ...")

    files = await github.list_repo_files(owner, repo)
    print(f"    Root files: {files}")

    missing = [f for f in EXPECTED_FILES if f not in files]
    if missing:
        print(f"    FAIL: missing expected files: {missing}")
        return False

    # Check .github/workflows exists
    workflow_files = await github.list_repo_files(owner, repo, path=".github/workflows")
    print(f"    Workflow files: {workflow_files}")
    if not workflow_files:
        print("    FAIL: .github/workflows is empty")
        return False

    print("    All expected files present")
    return True


async def _cleanup_repo(github, repo_full):
    """Delete the test GitHub repo."""
    owner, repo = repo_full.split("/")
    print(f"[7] Deleting repo {repo_full} ...")
    try:
        deleted = await github.delete_repo(owner, repo)
        if deleted:
            print("    Repo deleted")
        else:
            print("    Repo not found (already deleted?)")
    except Exception as e:
        print(f"    WARNING: failed to delete repo: {e}")


async def main():
    request_id = str(uuid.uuid4())
    print(f"[1] Request ID: {request_id}")

    github = GitHubAppClient()
    await _create_repo(github)

    print("[3] Getting GitHub App token ...")
    token = await github.get_token(GITHUB_ORG, TEST_REPO_NAME)
    print(f"    Token: {token[:10]}...{token[-4:]}")

    repo_full = f"{GITHUB_ORG}/{TEST_REPO_NAME}"
    worker_name = f"dev-scaffold-e2e-{request_id[:8]}"

    scaffold_config = ScaffoldConfig(
        template_repo="gh:vladmesh/service-template",
        project_name="scaffold-e2e-test",
        modules="backend",
        task_description="Simple REST API for managing TODO items",
    )

    cmd = CreateWorkerCommand(
        request_id=request_id,
        config=WorkerConfig(
            name=worker_name,
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="You are a developer. Read TASK.md and implement.",
            task_content="Build a simple TODO API.",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT, WorkerCapability.GITHUB_CLI],
            env_vars={"GITHUB_TOKEN": token, "REPO_NAME": repo_full},
            scaffold_config=scaffold_config,
        ),
        context={"source": "e2e-test", "repo": repo_full},
    )

    print(f"[4] Publishing CreateWorkerCommand to {COMMAND_STREAM} ...")
    print(f"    worker_name: {worker_name}")
    print(f"    scaffold: template={scaffold_config.template_repo}")
    print(f"    project={scaffold_config.project_name} modules={scaffold_config.modules}")

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    passed = False
    worker_id = None

    try:
        await r.xadd(COMMAND_STREAM, {"data": cmd.model_dump_json()})
        print("    Published!")

        print("[5] Waiting for CreateWorkerResponse (300s timeout) ...")
        group = f"e2e-test-{request_id[:8]}"
        consumer = f"e2e-{request_id[:8]}"

        try:
            await r.xgroup_create(RESPONSE_STREAM, group, id="$", mkstream=True)
        except Exception as e:  # noqa: BLE001
            if "BUSYGROUP" not in str(e):
                raise

        create_response = await _wait_for_response(r, group, consumer, request_id)

        if not create_response:
            print("    TIMEOUT — no response from worker-manager")
        elif not create_response.get("success"):
            print(f"    FAILED: {create_response.get('error')}")
        else:
            worker_id = create_response.get("worker_id")
            print(f"    SUCCESS! Worker created: {worker_id}")

            passed = await _verify_scaffold(github, repo_full)

        try:
            await r.xgroup_destroy(RESPONSE_STREAM, group)
        except Exception:  # noqa: S110, BLE001
            pass

    finally:
        await r.aclose()

    # Always cleanup GitHub repo
    await _cleanup_repo(github, repo_full)

    # Print worker_id for container cleanup by Makefile
    if worker_id:
        print(f"WORKER_ID={worker_id}")

    # Final verdict
    if passed:
        print("\n=== E2E SCAFFOLD TEST: PASSED ===")
    else:
        print("\n=== E2E SCAFFOLD TEST: FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
