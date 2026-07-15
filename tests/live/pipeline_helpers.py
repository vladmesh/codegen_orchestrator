"""Shared helpers for pipeline live tests.

Extracted from conftest.py so test modules can import them directly.
These are plain functions, not pytest fixtures.
"""

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

import httpx
from live_harness import CleanupError, OwnershipManifest, cleanup_on_error, resolve_repo_root

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.queues import SCAFFOLD_QUEUE

# ── Constants ────────────────────────────────────────────────────────────
API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
AUTH_HEADERS = {"X-Telegram-ID": str(TEST_TELEGRAM_ID)}

GITHUB_ORG = "project-factory-organization"
TEMPLATE_REPO = "gh:vladmesh/service-template"
TEMPLATE_REF = "0.3.0"
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))

# Timeouts (seconds)
SCAFFOLD_TIMEOUT = 120
ENGINEERING_TIMEOUT = 420  # 7 min (worker spawn + noop + CI)
DEPLOY_TIMEOUT = 420  # 7 min (deploy.yml + smoke test)
SCAFFOLD_FENCE_TIMEOUT = 900


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
            "status": ProjectStatus.DRAFT,
            "config": {
                "description": "Pipeline E2E test — noop",
                "modules": ["backend"],
                "agent_type": "noop",
            },
        },
    )
    assert resp.status_code == 201, f"Create project failed: {resp.text}"

    manifest = OwnershipManifest(run_id=project_id)
    manifest.own("project", project_id)
    ctx = {
        "project_id": project_id,
        "project_name": project_name,
        "repo_name": project_name,
        "manifest": manifest,
    }

    async with cleanup_on_error(lambda: cleanup_all(api, None, ctx)):
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")
        resp = await api.post(
            "/api/repositories/",
            json={
                "project_id": project_id,
                "name": project_name,
                "git_url": f"https://github.com/{GITHUB_ORG}/{project_name}",
            },
        )
        assert resp.status_code == 201, f"Create repository failed: {resp.text}"
        ctx["repo_id"] = resp.json()["id"]

    return ctx


def trigger_scaffold(ctx: dict) -> None:
    """Publish scaffold message to Redis stream."""
    ctx["manifest"].own("github_repository", f"{GITHUB_ORG}/{ctx['repo_name']}")
    ctx["manifest"].own("registry_repository", f"{GITHUB_ORG}/{ctx['repo_name']}-backend")
    ctx["manifest"].write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json")
    msg = {
        "project_id": ctx["project_id"],
        "repository_id": ctx["repo_id"],
        "user_id": "live-test",
        "template_repo": TEMPLATE_REPO,
        "template_ref": TEMPLATE_REF,
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
    entry_id = result.stdout.strip()
    ctx["manifest"].own("redis_entry", entry_id, stream=SCAFFOLD_QUEUE)
    ctx["manifest"].write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json")


async def wait_scaffold(api: httpx.AsyncClient, ctx: dict, timeout: int = SCAFFOLD_TIMEOUT) -> None:
    """Wait for scaffold to complete. Updates ctx['scaffold_status'].

    After ProjectStatus split (#22), scaffold success sets status to 'active'.
    Failure leaves status as 'draft' — we detect that via timeout.
    """
    status = await poll_status(
        api,
        f"/api/projects/{ctx['project_id']}",
        {ProjectStatus.ACTIVE},
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
            "status": TaskStatus.BACKLOG,
        },
    )
    assert resp.status_code == 201, f"Create task failed: {resp.text}"
    ctx["task_id"] = resp.json()["id"]

    resp = await api.post(
        f"/api/tasks/{ctx['task_id']}/transition",
        params={"to_status": TaskStatus.TODO},
        json={"actor": "live-test"},
    )
    assert resp.status_code == 200, f"Task transition to todo failed: {resp.text}"


async def wait_engineering(
    api: httpx.AsyncClient, ctx: dict, timeout: int = ENGINEERING_TIMEOUT
) -> None:
    """Wait for engineering to complete. Updates ctx['task_status'], ctx['story_status']."""
    done_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
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

    # Wait for story to progress (scheduler complete_stories cycle ~30s)
    # With PR-based CI gate, story goes to PR_REVIEW (not DEPLOYING) after all tasks done.
    # PR_REVIEW → DEPLOYING happens later via webhook when PR is merged.
    if "story_id" in ctx and status == TaskStatus.DONE:
        for _ in range(20):  # up to 60s
            await asyncio.sleep(3)
            resp = await api.get(f"/api/stories/{ctx['story_id']}")
            if resp.status_code == 200:
                story_status = resp.json().get("status")
                ctx["story_status"] = story_status
                if story_status in {
                    StoryStatus.PR_REVIEW,
                    StoryStatus.DEPLOYING,
                    StoryStatus.COMPLETED,
                    StoryStatus.FAILED,
                }:
                    break
    elif "story_id" in ctx:
        resp = await api.get(f"/api/stories/{ctx['story_id']}")
        if resp.status_code == 200:
            ctx["story_status"] = resp.json().get("status")


async def poll_field(
    api: httpx.AsyncClient,
    endpoint: str,
    field: str,
    target_values: set[str],
    timeout: int,
) -> str | None:
    """Poll an API endpoint until field is in target_values or timeout."""
    value = None
    for _ in range(timeout // 3):
        await asyncio.sleep(3)
        resp = await api.get(endpoint)
        if resp.status_code != 200:
            continue
        value = resp.json().get(field)
        if value in target_values:
            return value
    return value


async def wait_deploy(
    api: httpx.AsyncClient,
    api_no_auth: httpx.AsyncClient,
    ctx: dict,
    timeout: int = DEPLOY_TIMEOUT,
) -> None:
    """Wait for deploy to complete. Updates ctx with deployment info.

    Polls Application status (via repositories) instead of project.service_status.
    """
    terminal = {
        ApplicationStatus.RUNNING,
        ApplicationStatus.DOWN,
        ApplicationStatus.DEGRADED,
    }
    app_status = None
    application = None
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        repos_resp = await api.get("/api/repositories/", params={"project_id": ctx["project_id"]})
        for repo in repos_resp.json():
            apps_resp = await api.get("/api/applications/", params={"repo_id": repo["id"]})
            for app in apps_resp.json():
                if app["status"] in {s.value for s in terminal}:
                    app_status = app["status"]
                    application = app
                    break
            if app_status:
                break
        if app_status:
            break
        await asyncio.sleep(5)

    ctx["final_app_status"] = app_status

    if app_status != ApplicationStatus.RUNNING.value:
        return

    if application is None:
        return

    # Port allocations belong to an application, not directly to a project.
    resp = await api_no_auth.get("/api/servers/")
    for srv in resp.json():
        resp = await api_no_auth.get(f"/api/servers/{srv['handle']}/ports")
        for alloc in resp.json():
            if alloc.get("application_id") == application["id"]:
                ctx["server_ip"] = srv["public_ip"]
                ctx["port"] = alloc["port"]
                ctx["allocation_id"] = alloc["id"]
                ctx["application_id"] = application["id"]
                ctx["server_handle"] = srv["handle"]
                ctx["manifest"].own(
                    "server_deployment",
                    ctx["project_name"],
                    server_handle=srv["handle"],
                    server_ip=srv["public_ip"],
                )
                ctx["manifest"].own("port_allocation", str(alloc["id"]))
                ctx["manifest"].write(
                    ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
                )
                break
        if "port" in ctx:
            break

    if "port" in ctx:
        ctx["deployed_url"] = f"http://{ctx['server_ip']}:{ctx['port']}"


# ── Cleanup helpers ──────────────────────────────────────────────────────


def _redis_command(*args: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", *args],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def cancel_and_wait_for_scaffold(
    project_id: str,
    *,
    command=_redis_command,
    timeout: float = SCAFFOLD_FENCE_TIMEOUT,
    poll_interval: float = 1,
) -> None:
    """Fence new scaffold work and wait until claimed work is quiescent."""
    cancel_key = f"live:scaffold:cancelled:{project_id}"
    leases_key = f"live:scaffold:leases:{project_id}"
    command("SET", cancel_key, "1", "EX", str(SCAFFOLD_FENCE_TIMEOUT))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = command(
            "EVAL",
            "local t=redis.call('TIME'); local n=t[1]*1000+math.floor(t[2]/1000); "
            "redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',n); "
            "return redis.call('ZCARD',KEYS[1])",
            "1",
            leases_key,
        )
        if active == "0":
            return
        time.sleep(poll_interval)
    raise CleanupError(f"scaffold work for project {project_id} did not terminate")


def cancel_owned_scaffold(ctx: dict) -> None:
    """Fence scaffold work when the context owns a project."""
    project_id = ctx.get("project_id")
    if project_id:
        cancel_and_wait_for_scaffold(project_id)


def build_github_cleanup_script(repo_name: str) -> str:
    """Build the container-side cleanup for one exact owned repository."""
    return (
        "import asyncio, sys\n"
        "sys.path.insert(0, '/app')\n"
        "from shared.clients.github import GitHubAppClient\n"
        "import httpx\n"
        "async def cleanup():\n"
        "    gh = GitHubAppClient()\n"
        f"    token = await gh.get_org_token('{GITHUB_ORG}')\n"
        "    async with httpx.AsyncClient() as client:\n"
        "            resp = await client.delete(\n"
        f"                'https://api.github.com/repos/{GITHUB_ORG}/{repo_name}',\n"
        "                headers={'Authorization': f'token {token}',\n"
        "                         'Accept': 'application/vnd.github+json'},\n"
        "            )\n"
        "            if resp.status_code not in (204, 404):\n"
        "                raise RuntimeError(f'{resp.status_code} {resp.text[:200]}')\n"
        "            verify = await client.get(\n"
        f"                'https://api.github.com/repos/{GITHUB_ORG}/{repo_name}',\n"
        "                headers={'Authorization': f'token {token}'},\n"
        "            )\n"
        "            if verify.status_code != 404:\n"
        "                raise RuntimeError(f'repository residue: {verify.status_code}')\n"
        "asyncio.run(cleanup())\n"
    )


def cleanup_github_repo(repo_name: str) -> None:
    """Delete and verify one GitHub repo via the container's org token."""
    script = build_github_cleanup_script(repo_name)
    result = docker_exec("langgraph", script, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def build_registry_cleanup_script(repository: str) -> str:
    """Build fail-closed cleanup for one manifest-owned registry repository."""
    return (
        "import asyncio, os\n"
        "import httpx\n"
        f"repository = {repository!r}\n"
        "registry = os.environ.get('ORCHESTRATOR_HOSTNAME')\n"
        "username = os.environ.get('REGISTRY_USER')\n"
        "password = os.environ.get('REGISTRY_PASSWORD')\n"
        "if not registry or not username or not password:\n"
        "    raise RuntimeError('registry cleanup credentials are not configured')\n"
        "base = registry if registry.startswith(('http://', 'https://')) else f'https://{registry}'\n"
        "base = base.rstrip('/')\n"
        "headers = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}\n"
        "async def cleanup():\n"
        "    async with httpx.AsyncClient(auth=(username, password), timeout=20) as client:\n"
        "        tags = await client.get(f'{base}/v2/{repository}/tags/list')\n"
        "        if tags.status_code == 404:\n"
        "            return\n"
        "        tags.raise_for_status()\n"
        "        digests = set()\n"
        "        for tag in tags.json().get('tags') or []:\n"
        "            manifest_url = f'{base}/v2/{repository}/manifests/{tag}'\n"
        "            manifest = await client.get(manifest_url, headers=headers)\n"
        "            manifest.raise_for_status()\n"
        "            digest = manifest.headers.get('Docker-Content-Digest')\n"
        "            if not digest:\n"
        "                raise RuntimeError(f'manifest digest missing for {repository}:{tag}')\n"
        "            digests.add(digest)\n"
        "        for digest in digests:\n"
        "            deleted = await client.delete(f'{base}/v2/{repository}/manifests/{digest}')\n"
        "            if deleted.status_code not in (202, 404):\n"
        "                deleted.raise_for_status()\n"
        "        verify = await client.get(f'{base}/v2/{repository}/tags/list')\n"
        "        if verify.status_code != 404 and (verify.json().get('tags') or []):\n"
        "            raise RuntimeError(f'registry tags remain for {repository}')\n"
        "asyncio.run(cleanup())\n"
    )


def cleanup_registry_repository(repository: str) -> None:
    """Delete and verify registry artifacts recorded for one live run."""
    result = docker_exec("langgraph", build_registry_cleanup_script(repository), timeout=90)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def cleanup_registry_resources(ctx: dict, errors: list[str]) -> None:
    """Remove every registry repository explicitly recorded for this run."""
    for resource in ctx["manifest"].resources:
        if resource.kind != "registry_repository":
            continue
        try:
            cleanup_registry_repository(resource.identifier)
        except Exception as exc:
            errors.append(f"registry repository {resource.identifier}: {exc}")


def capture_owned_workers(ctx: dict) -> None:
    """Add workers locked to this run to its persisted ownership manifest."""
    project_id = ctx.get("project_id")
    if not project_id:
        return
    scan = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "redis",
            "redis-cli",
            "--scan",
            "--pattern",
            "worker:meta:*",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=ORCHESTRATOR_ROOT,
    )
    if scan.returncode != 0:
        raise RuntimeError(scan.stderr)
    for key in scan.stdout.splitlines():
        worker_id = key.removeprefix("worker:meta:")
        owner = subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "HGET", key, "project_id"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )
        if owner.returncode != 0:
            raise RuntimeError(owner.stderr)
        if owner.stdout.strip() != project_id:
            continue
        container = find_worker_container(worker_id)
        image = ""
        if container:
            inspect = subprocess.run(
                ["docker", "inspect", "--format", "{{.Config.Image}}", container],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=ORCHESTRATOR_ROOT,
            )
            image = inspect.stdout.strip() if inspect.returncode == 0 else ""
        ctx["manifest"].own("worker", worker_id, image=image, container=container)
    ctx["manifest"].write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json")


def cleanup_owned_workers(ctx: dict, errors: list[str]) -> None:
    """Remove run workers and verify their containers and Redis state are absent."""
    try:
        capture_owned_workers(ctx)
    except Exception as exc:
        errors.append(f"worker ownership discovery: {exc}")
        return
    for resource in ctx["manifest"].resources:
        if resource.kind != "worker":
            continue
        worker_id = resource.identifier
        container = resource.metadata.get("container") or find_worker_container(worker_id)
        if container:
            removed = subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=ORCHESTRATOR_ROOT,
            )
            if removed.returncode != 0 and "No such container" not in removed.stderr:
                errors.append(f"worker {worker_id}: {removed.stderr.strip()}")
            verify = subprocess.run(
                ["docker", "inspect", container],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=ORCHESTRATOR_ROOT,
            )
            if verify.returncode == 0:
                errors.append(f"worker {worker_id} container still exists")
        keys = [
            f"worker:status:{worker_id}",
            f"worker:meta:{worker_id}",
            f"worker:error:{worker_id}",
            f"worker:last_activity:{worker_id}",
            f"worker:{worker_id}:input",
            f"worker:{worker_id}:output",
        ]
        deleted = subprocess.run(
            ["docker", "compose", "exec", "-T", "redis", "redis-cli", "DEL", *keys],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )
        if deleted.returncode != 0:
            errors.append(f"worker {worker_id} Redis cleanup: {deleted.stderr.strip()}")
        # Capability images are deterministic hashes of agent type and capabilities.
        # They contain no run input and are deliberately safe to reuse between runs.


def find_worker_container(worker_id: str) -> str | None:
    """Resolve a worker container by Worker Manager's stable ownership label."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.codegen.worker.id={worker_id}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    names = [name for name in result.stdout.splitlines() if name]
    if len(names) > 1:
        raise RuntimeError(f"multiple containers claim worker {worker_id}")
    return names[0] if names else None


def cleanup_server_container(ctx: dict) -> None:
    """Stop and remove deployed container on remote server via SSH.

    Uses a multi-step approach matching how deploy.yml creates resources:
    1. docker compose -p {name} down (graceful, using both compose files)
    2. docker rm -f by container name pattern (force, catches leaked containers)
    3. verify no project-labelled container remains
    4. remove and verify `/opt/services/{name}`

    Logs warnings via structlog instead of silently swallowing errors.
    """
    if "server_handle" not in ctx:
        return
    project_name = ctx["project_name"]
    server_ip = ctx.get("server_ip", "127.0.0.1")
    server_handle = ctx["server_handle"]

    # Shell script that runs on the remote server
    # Step 1: try compose down with project name (matches deploy.yml invocation)
    # Step 2: force-remove any containers with matching project label
    # Step 3: prune dangling networks left by compose
    # Step 4: remove service directory
    remote_cmd = (
        f"set -e; "
        f"SVC_DIR=/opt/services/{project_name}; "
        f'if [ -d "$SVC_DIR/infra" ]; then '
        f"  cd $SVC_DIR/infra && "
        f"  docker compose -p {project_name} down --remove-orphans -v; "
        f"fi; "
        f"for c in $(docker ps -aq --filter label=com.docker.compose.project={project_name}); do "
        f"  docker rm -f $c; "
        f"done; "
        f"rm -rf $SVC_DIR; "
        f'test -z "$(docker ps -aq --filter label=com.docker.compose.project={project_name})"; '
        f"test ! -e $SVC_DIR"
    )

    script = (
        "import asyncio, sys, os\n"
        "sys.path.insert(0, '/app')\n"
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "async def main():\n"
        "    import httpx\n"
        "    api_url = os.environ.get('API_URL', 'http://api:8000')\n"
        "    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:\n"
        f"        resp = await client.get('/api/servers/{server_handle}/ssh-key')\n"
        "        if resp.status_code != 200:\n"
        "            raise RuntimeError(f'ssh key fetch failed: {resp.status_code}')\n"
        "        key = resp.json().get('ssh_key', '')\n"
        "    if not key.endswith('\\n'):\n"
        "        key += '\\n'\n"
        "    import tempfile, subprocess\n"
        "    with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:\n"
        "        f.write(key)\n"
        "        key_path = f.name\n"
        "    os.chmod(key_path, 0o600)\n"
        "    try:\n"
        "        result = subprocess.run(\n"
        "            ['ssh', '-i', key_path, '-o', 'StrictHostKeyChecking=no',\n"
        "             '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes',\n"
        f"             'root@{server_ip}',\n"
        f"             {repr(remote_cmd)}],\n"
        "            capture_output=True, text=True, timeout=60,\n"
        "        )\n"
        "        if result.returncode != 0:\n"
        "            raise RuntimeError(\n"
        "                f'cleanup ssh failed: {result.returncode} {result.stderr[:300]}'\n"
        "            )\n"
        "        else:\n"
        f"            logger.info('cleanup_server_done', "
        f"project='{project_name}', server='{server_ip}')\n"
        "    finally:\n"
        "        os.unlink(key_path)\n"
        "asyncio.run(main())\n"
    )
    result = docker_exec("langgraph", script, timeout=75)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


async def cleanup_all(
    api: httpx.AsyncClient,
    api_no_auth: httpx.AsyncClient | None,
    ctx: dict,
) -> None:
    """Delete only resources owned by this run and prove absence."""
    errors: list[str] = []

    # XDEL cannot cancel a claimed message. Fence the consumer and wait for any
    # active scaffold job before deleting or verifying external resources.
    try:
        cancel_owned_scaffold(ctx)
    except Exception as exc:
        errors.append(f"scaffold cancellation fence: {exc}")
        raise CleanupError("owned-resource cleanup failed: " + "; ".join(errors)) from exc

    # 1. Server container (if deployed)
    try:
        cleanup_server_container(ctx)
    except Exception as exc:
        errors.append(f"server deployment: {exc}")

    cleanup_owned_workers(ctx, errors)

    # 2. Port allocation
    if "allocation_id" in ctx and api_no_auth:
        try:
            resp = await api_no_auth.delete(f"/api/allocations/{ctx['allocation_id']}")
            if resp.status_code not in (200, 204, 404):
                raise RuntimeError(f"delete returned {resp.status_code}")
            ports = await api_no_auth.get(f"/api/servers/{ctx['server_handle']}/ports")
            ports.raise_for_status()
            if any(str(item["id"]) == str(ctx["allocation_id"]) for item in ports.json()):
                raise RuntimeError("allocation still exists")
        except Exception as exc:
            errors.append(f"port allocation: {exc}")

    # 3. Registry images created by CI for this run.
    owned_kinds = {resource.kind for resource in ctx["manifest"].resources}
    cleanup_registry_resources(ctx, errors)

    # 4. GitHub repo
    if "github_repository" in owned_kinds:
        try:
            cleanup_github_repo(ctx["repo_name"])
        except Exception as exc:
            errors.append(f"GitHub repository: {exc}")

    # 5. DB records (API delete doesn't cascade to stories/tasks, use SQL)
    if "project_id" in ctx:
        try:
            _cleanup_db(ctx["project_id"])
        except Exception as exc:
            errors.append(f"database project: {exc}")

    for resource in ctx.get("manifest", OwnershipManifest("missing")).resources:
        if resource.kind != "redis_entry":
            continue
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "XDEL",
                resource.metadata["stream"],
                resource.identifier,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )
        if result.returncode != 0:
            errors.append(f"Redis entry {resource.identifier}: {result.stderr}")
            continue
        verify = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "redis-cli",
                "XRANGE",
                resource.metadata["stream"],
                resource.identifier,
                resource.identifier,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )
        if verify.returncode != 0 or verify.stdout.strip():
            errors.append(f"Redis entry {resource.identifier} still exists or cannot be verified")

    if "project_id" in ctx:
        verify = await api.get(f"/api/projects/{ctx['project_id']}")
        if verify.status_code != 404:
            errors.append(f"project {ctx['project_id']} still exists")
    if errors:
        raise CleanupError("owned-resource cleanup failed: " + "; ".join(errors))
    manifest_path = ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
    manifest_path.unlink(missing_ok=True)


def _cleanup_db(project_id: str) -> None:
    """Delete project and all related records via SQL (proper cascade)."""
    sql = (
        f"DELETE FROM task_events WHERE task_id IN "
        f"(SELECT id FROM tasks WHERE project_id = '{project_id}');"
        f"DELETE FROM runs WHERE project_id = '{project_id}';"
        f"DELETE FROM tasks WHERE project_id = '{project_id}';"
        f"DELETE FROM stories WHERE project_id = '{project_id}';"
        f"DELETE FROM brainstorms WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_chunks WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_documents WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_conversation_summaries WHERE project_id = '{project_id}';"
        f"DELETE FROM rag_messages WHERE project_id = '{project_id}';"
        f"DELETE FROM service_deployments WHERE project_id = '{project_id}';"
        f"DELETE FROM port_allocations WHERE application_id IN "
        f"(SELECT id FROM applications WHERE repo_id IN "
        f"(SELECT id FROM repositories WHERE project_id = '{project_id}'));"
        f"DELETE FROM applications WHERE repo_id IN "
        f"(SELECT id FROM repositories WHERE project_id = '{project_id}');"
        f"DELETE FROM repositories WHERE project_id = '{project_id}';"
        f"DELETE FROM projects WHERE id = '{project_id}';"
    )
    result = subprocess.run(
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
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


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
        f"- final_app_status: `{ctx.get('final_app_status')}`",
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
