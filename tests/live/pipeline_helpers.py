"""Shared helpers for pipeline live tests.

Extracted from conftest.py so test modules can import them directly.
These are plain functions, not pytest fixtures.
"""

import asyncio
from datetime import UTC, datetime
import os
import secrets
import subprocess
import uuid

import httpx

# ── Constants ────────────────────────────────────────────────────────────
API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
AUTH_HEADERS = {"X-Telegram-ID": str(TEST_TELEGRAM_ID)}

GITHUB_ORG = "project-factory-organization"
TEMPLATE_REPO = "gh:project-factory-organization/service-template"
SCAFFOLD_QUEUE = "scaffold:queue"
ORCHESTRATOR_ROOT = "/home/vlad/projects/codegen_orchestrator"

# Timeouts (seconds)
SCAFFOLD_TIMEOUT = 120
ENGINEERING_TIMEOUT = 420  # 7 min (worker spawn + noop + CI)
DEPLOY_TIMEOUT = 420  # 7 min (deploy.yml + smoke test)


# ── Low-level helpers ────────────────────────────────────────────────────


def docker_exec(service: str, script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a Python script inside a docker compose service."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ORCHESTRATOR_ROOT,
    )


async def ensure_test_user(api: httpx.AsyncClient) -> None:
    """Create test user if not exists."""
    await api.post(
        "/api/users/upsert",
        json={
            "telegram_id": TEST_TELEGRAM_ID,
            "username": "live_test_bot",
            "first_name": "Live",
            "last_name": "Test",
        },
    )


async def poll_status(
    api: httpx.AsyncClient,
    endpoint: str,
    target_statuses: set[str],
    timeout: int,
) -> str | None:
    """Poll an API endpoint until status is in target_statuses or timeout."""
    status = None
    for _ in range(timeout // 3):
        await asyncio.sleep(3)
        resp = await api.get(endpoint)
        if resp.status_code != 200:
            continue
        status = resp.json().get("status")
        if status in target_statuses:
            return status
    return status


# ── Pipeline phase helpers ───────────────────────────────────────────────


async def create_noop_project(api: httpx.AsyncClient) -> dict:
    """Create project + repository for noop pipeline testing. Returns ctx dict."""
    suffix = secrets.token_hex(4)
    project_name = f"live-test-{suffix}"
    project_id = str(uuid.uuid4())

    resp = await api.post(
        "/api/projects/",
        json={
            "id": project_id,
            "name": project_name,
            "status": "draft",
            "config": {
                "description": "Pipeline E2E test — noop",
                "modules": ["backend"],
                "agent_type": "noop",
            },
        },
    )
    assert resp.status_code == 201, f"Create project failed: {resp.text}"

    resp = await api.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": project_name,
            "git_url": f"https://github.com/{GITHUB_ORG}/{project_name}",
        },
    )
    assert resp.status_code == 201, f"Create repository failed: {resp.text}"

    return {
        "project_id": project_id,
        "project_name": project_name,
        "repo_name": project_name,
        "repo_id": resp.json()["id"],
    }


def flush_queues() -> None:
    """Delete pipeline queues (stream + consumer groups + PEL).

    Using DEL instead of XTRIM — XTRIM removes messages but leaves
    the consumer group's PEL intact, causing consumers to hang on
    stale pending entries. DEL removes everything; consumers recreate
    the stream via XGROUP CREATE ... MKSTREAM on next startup.
    """
    for queue in [SCAFFOLD_QUEUE, "engineering:queue", "deploy:queue"]:
        subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "DEL", queue],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )


def trigger_scaffold(ctx: dict) -> None:
    """Publish scaffold message to Redis stream."""
    msg = {
        "project_id": ctx["project_id"],
        "repository_id": ctx["repo_id"],
        "user_id": "live-test",
        "template_repo": TEMPLATE_REPO,
        "project_name": ctx["project_name"],
        "modules": "backend",
        "task_description": "Pipeline E2E test project",
    }
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
            *[item for pair in msg.items() for item in pair],
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=ORCHESTRATOR_ROOT,
    )
    assert result.returncode == 0, f"XADD scaffold failed: {result.stderr}"


async def wait_scaffold(api: httpx.AsyncClient, ctx: dict, timeout: int = SCAFFOLD_TIMEOUT) -> None:
    """Wait for scaffold to complete. Updates ctx['scaffold_status']."""
    status = await poll_status(
        api,
        f"/api/projects/{ctx['project_id']}",
        {"scaffolded", "scaffold_failed"},
        timeout,
    )
    ctx["scaffold_status"] = status


async def create_story_and_task(api: httpx.AsyncClient, ctx: dict) -> None:
    """Create story (in_progress) + task (todo) for engineering pipeline."""
    resp = await api.post(
        "/api/stories/",
        json={
            "project_id": ctx["project_id"],
            "title": "Pipeline test story",
            "description": "Automated pipeline test",
            "type": "technical",
        },
    )
    assert resp.status_code == 201, f"Create story failed: {resp.text}"
    ctx["story_id"] = resp.json()["id"]

    resp = await api.post(
        f"/api/stories/{ctx['story_id']}/start",
        json={"actor": "live-test"},
    )
    assert resp.status_code == 200, f"Story start failed: {resp.text}"

    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": ctx["project_id"],
            "story_id": ctx["story_id"],
            "type": "create",
            "title": "Noop implementation task",
            "description": "Empty commit via NoopRunner — pipeline test",
            "status": "backlog",
        },
    )
    assert resp.status_code == 201, f"Create task failed: {resp.text}"
    ctx["task_id"] = resp.json()["id"]

    resp = await api.post(
        f"/api/tasks/{ctx['task_id']}/transition",
        params={"to_status": "todo"},
        json={"actor": "live-test"},
    )
    assert resp.status_code == 200, f"Task transition to todo failed: {resp.text}"


async def wait_engineering(
    api: httpx.AsyncClient, ctx: dict, timeout: int = ENGINEERING_TIMEOUT
) -> None:
    """Wait for engineering to complete. Updates ctx['task_status'], ctx['story_status']."""
    done_statuses = {"done", "failed", "cancelled"}
    status = None
    elapsed = 0
    while elapsed < timeout:
        await asyncio.sleep(5)
        elapsed += 5
        resp = await api.get(f"/api/tasks/{ctx['task_id']}")
        status = resp.json().get("status")
        if status in done_statuses:
            break
    ctx["task_status"] = status
    ctx["engineering_elapsed"] = elapsed

    # Wait for story completion (scheduler complete_stories cycle ~30s)
    if "story_id" in ctx and status == "done":
        for _ in range(20):  # up to 60s
            await asyncio.sleep(3)
            resp = await api.get(f"/api/stories/{ctx['story_id']}")
            if resp.status_code == 200:
                story_status = resp.json().get("status")
                ctx["story_status"] = story_status
                if story_status in {"completed", "failed"}:
                    break
    elif "story_id" in ctx:
        resp = await api.get(f"/api/stories/{ctx['story_id']}")
        if resp.status_code == 200:
            ctx["story_status"] = resp.json().get("status")


async def wait_deploy(
    api: httpx.AsyncClient,
    api_no_auth: httpx.AsyncClient,
    ctx: dict,
    timeout: int = DEPLOY_TIMEOUT,
) -> None:
    """Wait for deploy to complete. Updates ctx with deployment info."""
    deploy_done = {"active", "error", "failed"}
    status = await poll_status(
        api,
        f"/api/projects/{ctx['project_id']}",
        deploy_done,
        timeout,
    )
    ctx["final_project_status"] = status

    if status != "active":
        return

    # Find port allocation
    resp = await api_no_auth.get("/api/servers/")
    for srv in resp.json():
        resp = await api_no_auth.get(f"/api/servers/{srv['handle']}/ports")
        for alloc in resp.json():
            if alloc.get("project_id") == ctx["project_id"]:
                ctx["server_ip"] = srv["public_ip"]
                ctx["port"] = alloc["port"]
                ctx["allocation_id"] = alloc["id"]
                ctx["server_handle"] = srv["handle"]
                break
        if "port" in ctx:
            break

    if "port" in ctx:
        ctx["deployed_url"] = f"http://{ctx['server_ip']}:{ctx['port']}"


# ── Cleanup helpers ──────────────────────────────────────────────────────


def cleanup_github_repo(repo_name: str) -> None:
    """Delete GitHub repo via GitHubAppClient inside langgraph container."""
    script = (
        "import asyncio, sys\n"
        "sys.path.insert(0, '/app')\n"
        "from shared.clients.github import GitHubAppClient\n"
        "async def cleanup():\n"
        "    gh = GitHubAppClient()\n"
        "    try:\n"
        f"        await gh.delete_repo('{GITHUB_ORG}', '{repo_name}')\n"
        "    except Exception as e:\n"
        "        print(f'cleanup warning: {e}')\n"
        "asyncio.run(cleanup())\n"
    )
    docker_exec("langgraph", script, timeout=30)


def cleanup_server_container(ctx: dict) -> None:
    """Stop and remove deployed container on remote server via SSH."""
    if "server_handle" not in ctx:
        return
    project_name = ctx["project_name"]
    server_ip = ctx.get("server_ip", "127.0.0.1")
    server_handle = ctx["server_handle"]

    script = (
        "import asyncio, sys, os\n"
        "sys.path.insert(0, '/app')\n"
        "async def main():\n"
        "    import httpx\n"
        "    api_url = os.environ.get('API_URL', 'http://api:8000')\n"
        "    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:\n"
        f"        resp = await client.get('/api/servers/{server_handle}/ssh-key')\n"
        "        key = resp.json().get('ssh_key', '')\n"
        "    if not key.endswith('\\n'):\n"
        "        key += '\\n'\n"
        "    import tempfile, subprocess\n"
        "    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:\n"
        "        f.write(key)\n"
        "        key_path = f.name\n"
        "    os.chmod(key_path, 0o600)\n"
        f"    name = '{project_name}'\n"
        "    subprocess.run(\n"
        "        ['ssh', '-i', key_path, '-o', 'StrictHostKeyChecking=no',\n"
        f"         'root@{server_ip}',\n"
        f"         f'cd /opt/services/{{name}}/infra && docker compose down --remove-orphans -v "
        f"2>/dev/null; rm -rf /opt/services/{{name}}'],\n"
        "        capture_output=True, text=True, timeout=30,\n"
        "    )\n"
        "    os.unlink(key_path)\n"
        "asyncio.run(main())\n"
    )
    docker_exec("langgraph", script, timeout=45)


async def cleanup_all(
    api: httpx.AsyncClient,
    api_no_auth: httpx.AsyncClient | None,
    ctx: dict,
) -> None:
    """Full cleanup: server -> port allocation -> GitHub repo -> DB records.

    Always runs (success or failure). Errors are swallowed to avoid masking
    the original test failure.
    """
    # 1. Server container (if deployed)
    try:
        cleanup_server_container(ctx)
    except Exception:
        pass

    # 2. Port allocation
    if "allocation_id" in ctx and api_no_auth:
        try:
            await api_no_auth.delete(f"/api/allocations/{ctx['allocation_id']}")
        except Exception:
            pass

    # 3. GitHub repo
    if "repo_name" in ctx:
        try:
            cleanup_github_repo(ctx["repo_name"])
        except Exception:
            pass

    # 4. DB records (API delete doesn't cascade to stories/tasks, use SQL)
    if "project_id" in ctx:
        try:
            _cleanup_db(ctx["project_id"])
        except Exception:
            pass


def _cleanup_db(project_id: str) -> None:
    """Delete project and all related records via SQL (proper cascade)."""
    sql = (
        f"DELETE FROM task_events WHERE task_id IN "
        f"(SELECT id FROM tasks WHERE project_id = '{project_id}');"
        f"DELETE FROM runs WHERE project_id = '{project_id}';"
        f"DELETE FROM tasks WHERE project_id = '{project_id}';"
        f"DELETE FROM stories WHERE project_id = '{project_id}';"
        f"DELETE FROM repositories WHERE project_id = '{project_id}';"
        f"DELETE FROM port_allocations WHERE project_id = '{project_id}';"
        f"DELETE FROM projects WHERE id = '{project_id}';"
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "orchestrator",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )


# ── Debug dump ───────────────────────────────────────────────────────────


def dump_debug(ctx: dict, test_name: str) -> None:
    """Write debug info to docs/e2e_results/ for post-mortem analysis."""
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    filepath = os.path.join(ORCHESTRATOR_ROOT, "docs", "e2e_results", f"debug-{test_name}-{ts}.md")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    lines = [
        f"# Debug: {test_name}",
        f"**Time**: {datetime.now(tz=UTC).isoformat()}",
        "",
        "## Context",
        f"- project_id: `{ctx.get('project_id')}`",
        f"- project_name: `{ctx.get('project_name')}`",
        f"- scaffold_status: `{ctx.get('scaffold_status')}`",
        f"- task_id: `{ctx.get('task_id')}`",
        f"- task_status: `{ctx.get('task_status')}`",
        f"- story_status: `{ctx.get('story_status')}`",
        f"- final_project_status: `{ctx.get('final_project_status')}`",
        f"- deployed_url: `{ctx.get('deployed_url')}`",
        f"- engineering_elapsed: `{ctx.get('engineering_elapsed')}`",
        "",
    ]

    # Collect docker logs from relevant services
    for service in ["scaffolder", "engineering-worker", "scheduler", "deploy-worker"]:
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", "--tail=30", service],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=ORCHESTRATOR_ROOT,
            )
            if result.stdout.strip():
                lines.extend(
                    [
                        f"## {service} logs (last 30)",
                        "```",
                        result.stdout.strip()[-2000:],
                        "```",
                        "",
                    ]
                )
        except Exception:
            pass

    with open(filepath, "w") as f:
        f.write("\n".join(lines))
