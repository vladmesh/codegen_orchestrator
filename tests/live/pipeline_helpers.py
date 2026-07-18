"""Shared helpers for pipeline live tests.

Extracted from conftest.py so test modules can import them directly.
These are plain functions, not pytest fixtures.
"""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time
import uuid

from capability_cleanup import CapabilityMessage, cleanup_owned_capability_messages
import httpx
from live_harness import CleanupError, OwnershipManifest, cleanup_on_error, resolve_repo_root
from pydantic import ValidationError

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.qa import QAOutcome
from shared.queues import SCAFFOLD_QUEUE

# ── Constants ────────────────────────────────────────────────────────────
API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
USER_AUTH_HEADER = "X-Telegram-ID"
AUTH_HEADERS = {USER_AUTH_HEADER: str(TEST_TELEGRAM_ID)}

GITHUB_ORG = "project-factory-organization"
TEMPLATE_REPO = "gh:vladmesh/service-template"
TEMPLATE_REF = "0.3.5"
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))

# Timeouts (seconds)
SCAFFOLD_TIMEOUT = 120
ENGINEERING_TIMEOUT = 420  # 7 min (worker spawn + noop + CI)
LLM_ENGINEERING_TIMEOUT = 1800  # 30 min (worker spawn + LLM edits + CI-fix loop)
DEPLOY_TIMEOUT = 420  # 7 min (deploy.yml + smoke test)
# Deploy allocates a port per module: the web module plus these infra ports
# (mirror of deploy._DEPLOY_INFRA_PORT_SERVICES). Only the web module serves
# HTTP /health, so the deployed URL must not point at an infra port.
INFRA_PORT_SERVICES = frozenset({"postgres", "redis"})
SCAFFOLD_FENCE_TIMEOUT = 900
# Merged PR → pr_poller cycle → deploy run carrying the merged head SHA.
DEPLOY_RUN_TIMEOUT = 420
DEPLOY_RUN_POLL_INTERVAL = 5
# The deploy consumer writes the run result right after the app reports its
# status, so this only covers that last write.
DEPLOY_OUTCOME_TIMEOUT = 120
DEPLOY_OUTCOME_POLL_INTERVAL = 3
# Deploy hands off to QA on the scheduler's next poll, then QA retries the health
# check while the service finishes coming up.
QA_RUN_TIMEOUT = 300
QA_RUN_POLL_INTERVAL = 5
WORKER_REMOVAL_TIMEOUT = 15
WORKER_REMOVAL_POLL_INTERVAL = 0.25
RUN_CANCELLATION_TIMEOUT = 30
RUN_CANCELLATION_POLL_INTERVAL = 0.5
_ACTIVE_RUN_STATUSES = {"queued", "running"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}

# Deploy resolves its environment from the committed contract fragments, so the
# noop project must carry them. It selects only the backend module, and
# service-template renders exactly these owner fragments for that selection.
ENV_CONTRACT_FILENAME = "env.contract.yaml"
EXPECTED_ENV_CONTRACT_FRAGMENTS = frozenset(
    {
        "infra/env.contract.yaml",
        "services/backend/env.contract.yaml",
    }
)
# The probe prints one marked line so container log output cannot be parsed as
# its payload.
ENV_CONTRACT_PROBE_MARKER = "ENV_CONTRACT_PROBE:"

NOOP_PROJECT_DESCRIPTION = "Pipeline E2E test - noop"
NOOP_TASK_TITLE = "Noop implementation task"
NOOP_TASK_DESCRIPTION = "Empty commit via NoopRunner - pipeline test"

LLM_BACKEND_PROJECT_DESCRIPTION = (
    "Backend-only live LLM pipeline test. Build a minimal HTTP API that can deploy "
    "without any user-provided secrets."
)
LLM_BACKEND_DETAILED_SPEC = """Implement a backend-only service.

Requirements:
- Keep the project backend-only. Do not add frontend, Telegram, notification, or bot modules.
- Do not require any user-provided secrets or environment variables.
- Keep the existing deploy contract limited to generated, computed, or literal values.
- Ensure GET /health returns HTTP 200 with a small JSON payload.
- Add or update a focused backend test for the health endpoint if the scaffold does not already
  cover it.
- Run the repository's normal formatting, linting, and unit tests before committing.
"""
LLM_BACKEND_TASK_TITLE = "Implement backend health API"
LLM_BACKEND_TASK_DESCRIPTION = (
    "Use the scaffolded backend service and make the smallest code change needed to implement "
    "a production-safe GET /health endpoint that returns HTTP 200 JSON. The app must deploy "
    "with backend-only modules and no user-required secrets."
)


# ── Low-level helpers ────────────────────────────────────────────────────


def internal_headers() -> dict[str, str]:
    """Auth headers for internal-service endpoints, as the real consumers send them.

    /api/servers/* is gated by require_internal_or_admin, and the harness user is
    not admin, so without this header those endpoints answer 401/403.

    This key alone does not make /api/runs/ show every run: list_runs still
    narrows its result to the caller's own runs whenever it sees a non-admin
    X-Telegram-ID. Unowned runs are only visible to a client that sends no user
    header at all — see ``require_unscoped_run_observer``.
    """
    return {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}


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
    resp = await api.post(
        "/api/users/upsert",
        json={
            "telegram_id": TEST_TELEGRAM_ID,
            "username": "live_test_bot",
            "first_name": "Live",
            "last_name": "Test",
        },
    )
    resp.raise_for_status()


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
        resp.raise_for_status()
        status = resp.json().get("status")
        if status in target_statuses:
            return status
    return status


# ── Pipeline phase helpers ───────────────────────────────────────────────


async def create_pipeline_project(
    api: httpx.AsyncClient,
    api_internal: httpx.AsyncClient,
    *,
    project_prefix: str,
    description: str,
    agent_type: str,
    task_title: str,
    task_description: str,
    detailed_spec: str | None = None,
) -> dict:
    """Create project + repository for one live pipeline variant. Returns ctx dict."""
    suffix = secrets.token_hex(4)
    project_name = f"{project_prefix}-{suffix}"
    project_id = str(uuid.uuid4())
    config = {
        "description": description,
        "modules": ["backend"],
        "agent_type": agent_type,
    }
    if detailed_spec:
        config["detailed_spec"] = detailed_spec

    resp = await api.post(
        "/api/projects/",
        json={
            "id": project_id,
            "name": project_name,
            "status": ProjectStatus.DRAFT,
            "config": config,
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201, f"Create project failed: {resp.text}"

    manifest = OwnershipManifest(run_id=project_id)
    manifest.own("project", project_id)
    ctx = {
        "project_id": project_id,
        "project_name": project_name,
        "repo_name": project_name,
        "manifest": manifest,
        "agent_type": agent_type,
        "scaffold_task_description": task_description,
        "task_title": task_title,
        "task_description": task_description,
    }

    async with cleanup_on_error(lambda: cleanup_all(api_internal, None, ctx)):
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{project_id}.json")
        resp = await api.post(
            "/api/repositories/",
            json={
                "project_id": project_id,
                "name": project_name,
                "git_url": f"https://github.com/{GITHUB_ORG}/{project_name}",
            },
        )
        resp.raise_for_status()
        assert resp.status_code == 201, f"Create repository failed: {resp.text}"
        ctx["repo_id"] = resp.json()["id"]

    return ctx


async def create_noop_project(api: httpx.AsyncClient, api_internal: httpx.AsyncClient) -> dict:
    """Create project + repository for noop pipeline testing. Returns ctx dict."""
    return await create_pipeline_project(
        api,
        api_internal,
        project_prefix="live-test",
        description=NOOP_PROJECT_DESCRIPTION,
        agent_type="noop",
        task_title=NOOP_TASK_TITLE,
        task_description=NOOP_TASK_DESCRIPTION,
    )


async def create_llm_backend_project(
    api: httpx.AsyncClient, api_internal: httpx.AsyncClient
) -> dict:
    """Create project + repository for the live LLM backend pipeline."""
    return await create_pipeline_project(
        api,
        api_internal,
        project_prefix="live-test-llm",
        description=LLM_BACKEND_PROJECT_DESCRIPTION,
        detailed_spec=LLM_BACKEND_DETAILED_SPEC,
        agent_type="claude",
        task_title=LLM_BACKEND_TASK_TITLE,
        task_description=LLM_BACKEND_TASK_DESCRIPTION,
    )


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
        "task_description": ctx.get("scaffold_task_description", "Pipeline E2E test project"),
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
    resp.raise_for_status()
    assert resp.status_code == 201, f"Create story failed: {resp.text}"
    ctx["story_id"] = resp.json()["id"]

    resp = await api.post(
        f"/api/stories/{ctx['story_id']}/start",
        json={"actor": "live-test"},
    )
    resp.raise_for_status()
    assert resp.status_code == 200, f"Story start failed: {resp.text}"

    resp = await api.post(
        "/api/tasks/",
        json={
            "project_id": ctx["project_id"],
            "story_id": ctx["story_id"],
            "type": "create",
            "title": ctx.get("task_title", NOOP_TASK_TITLE),
            "description": ctx.get("task_description", NOOP_TASK_DESCRIPTION),
            "status": TaskStatus.BACKLOG,
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201, f"Create task failed: {resp.text}"
    ctx["task_id"] = resp.json()["id"]

    resp = await api.post(
        f"/api/tasks/{ctx['task_id']}/transition",
        params={"to_status": TaskStatus.TODO},
        json={"actor": "live-test"},
    )
    resp.raise_for_status()
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
        resp.raise_for_status()
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
            resp.raise_for_status()
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
        resp.raise_for_status()
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
        resp.raise_for_status()
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
        repos_resp.raise_for_status()
        for repo in repos_resp.json():
            apps_resp = await api.get("/api/applications/", params={"repo_id": repo["id"]})
            apps_resp.raise_for_status()
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

    if story_id := ctx.get("story_id"):
        tasks_resp = await api.get("/api/tasks/", params={"story_id": story_id})
        tasks_resp.raise_for_status()
        ctx["ci_failure_evidence"] = [
            {
                "fix_task_id": task["id"],
                **task["failure_metadata"]["ci_failure"],
            }
            for task in tasks_resp.json()
            if (task.get("failure_metadata") or {}).get("ci_failure")
        ]

    if app_status != ApplicationStatus.RUNNING.value:
        return

    if application is None:
        return

    # Port allocations belong to an application, not directly to a project.
    # /api/servers/ and its ports need internal-service auth, so authenticate like
    # the real consumers. raise_for_status keeps a non-200 loud instead of iterating
    # an error body and crashing with TypeError before the deploy reaches the
    # ownership manifest.
    headers = internal_headers()
    resp = await api.get("/api/servers/", headers=headers)
    resp.raise_for_status()
    for srv in resp.json():
        resp = await api.get(f"/api/servers/{srv['handle']}/ports", headers=headers)
        resp.raise_for_status()
        for alloc in resp.json():
            # Skip the app's postgres/redis infra ports: only the web module
            # serves HTTP /health, so deployed_url must point at it, not at an
            # infra port that never answers the health gate.
            if alloc.get("service_name") in INFRA_PORT_SERVICES:
                continue
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


# ── Environment contract probes ──────────────────────────────────────────


def build_env_contract_probe_script(
    repo_name: str,
    ref: str,
    *,
    verify_merged_into_main: bool,
) -> str:
    """Build the container-side environment-contract probe for one repository ref.

    Runs inside langgraph for the GitHub App credentials and reads the contract
    the way devops.env_contract_loader does: list the tree at ``ref``, take every
    env.contract.yaml and merge the fragments. Deploy resolves the contract at
    one exact ref, so probing any other ref proves nothing about that deploy.

    With ``verify_merged_into_main`` the probe also compares ``ref`` against main
    and reports whether main already contains it.

    The probe reads a repository; it creates nothing and owns nothing.
    """
    return (
        "import asyncio, json, sys\n"
        "sys.path.insert(0, '/app')\n"
        "import httpx\n"
        "import yaml\n"
        "from shared.clients.github import GitHubAppClient\n"
        "from shared.contracts.env_contract import merge_env_contract_fragments\n"
        f"owner = {GITHUB_ORG!r}\n"
        f"repo = {repo_name!r}\n"
        f"ref = {ref!r}\n"
        f"verify_merged = {verify_merged_into_main!r}\n"
        "async def probe():\n"
        "    gh = GitHubAppClient()\n"
        "    paths = await gh.list_repo_files_recursive(owner, repo, ref)\n"
        f"    fragment_paths = sorted(p for p in paths if p.endswith({ENV_CONTRACT_FILENAME!r}))\n"
        "    fragments = []\n"
        "    for path in fragment_paths:\n"
        "        content = await gh.get_file_contents(owner, repo, path, ref)\n"
        "        if content is None:\n"
        "            raise RuntimeError(f'contract fragment disappeared: {path}')\n"
        "        fragments.append(yaml.safe_load(content))\n"
        "    contract = merge_env_contract_fragments(fragments) if fragments else None\n"
        "    entries = sorted(contract.entries) if contract else []\n"
        "    user_secret_entries = (\n"
        "        sorted(\n"
        "            key for key, entry in contract.entries.items()\n"
        "            if getattr(entry, 'source', None) == 'user_secret'\n"
        "        )\n"
        "        if contract else []\n"
        "    )\n"
        "    required_user_secret_entries = (\n"
        "        sorted(\n"
        "            key for key, entry in contract.entries.items()\n"
        "            if getattr(entry, 'source', None) == 'user_secret'\n"
        "            and getattr(entry, 'required', False)\n"
        "        )\n"
        "        if contract else []\n"
        "    )\n"
        "    merged_into_main = None\n"
        "    if verify_merged:\n"
        "        token = await gh.get_token(owner, repo)\n"
        "        async with httpx.AsyncClient(timeout=20) as client:\n"
        "            resp = await client.get(\n"
        "                f'https://api.github.com/repos/{owner}/{repo}/compare/main...{ref}',\n"
        "                headers={'Authorization': f'token {token}',\n"
        "                         'Accept': 'application/vnd.github+json'},\n"
        "            )\n"
        "            resp.raise_for_status()\n"
        "            merged_into_main = resp.json()['status'] in ('identical', 'behind')\n"
        "    payload = {'ref': ref, 'fragment_paths': fragment_paths,\n"
        "               'entries': entries, 'user_secret_entries': user_secret_entries,\n"
        "               'required_user_secret_entries': required_user_secret_entries,\n"
        "               'merged_into_main': merged_into_main}\n"
        f"    print({ENV_CONTRACT_PROBE_MARKER!r} + json.dumps(payload))\n"
        "asyncio.run(probe())\n"
    )


def parse_env_contract_probe(stdout: str) -> dict:
    """Read the probe payload out of the container's stdout."""
    for line in stdout.splitlines():
        if line.startswith(ENV_CONTRACT_PROBE_MARKER):
            return json.loads(line[len(ENV_CONTRACT_PROBE_MARKER) :])
    raise RuntimeError(f"environment contract probe printed no payload: {stdout[:300]}")


def probe_env_contract(repo_name: str, ref: str, *, verify_merged_into_main: bool = False) -> dict:
    """Read the committed environment contract of one repository ref."""
    script = build_env_contract_probe_script(
        repo_name, ref, verify_merged_into_main=verify_merged_into_main
    )
    result = docker_exec("langgraph", script, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"environment contract probe for {repo_name}@{ref} failed: "
            f"{result.stderr or result.stdout}"
        )
    return parse_env_contract_probe(result.stdout)


def _env_contract_failure(probe: dict, phase: str, verify_merged_into_main: bool) -> str | None:
    """Return why a probe fails the contract expectation, or None when it holds."""
    ref = probe["ref"]
    missing = sorted(EXPECTED_ENV_CONTRACT_FRAGMENTS - set(probe["fragment_paths"]))
    if missing:
        return (
            f"{phase}: environment contract fragments missing at {ref}: "
            f"{', '.join(missing)}; found {probe['fragment_paths']}"
        )
    if not probe["entries"]:
        return f"{phase}: environment contract at {ref} declares no entries"
    if verify_merged_into_main and not probe["merged_into_main"]:
        return f"{phase}: {ref} is not contained in main — deploy would run an unmerged tree"
    return None


def record_env_contract(
    ctx: dict,
    ref: str,
    *,
    phase: str,
    verify_merged_into_main: bool = False,
) -> bool:
    """Probe one ref and record the result. True when the expectation holds.

    The probe lands in ``ctx['env_contract_probes'][phase]`` before it is judged,
    so a failing phase still leaves the observed paths in the debug dump.

    A probe that cannot run at all — GitHub 5xx, an unparseable fragment, a dead
    or slow container — is recorded as that phase's error rather than raised. The
    mega still fails on it, but through the same record-and-report path as a
    contract that merely misses a fragment, so the caller reaches its debug dump
    instead of losing the artifact to an exception.
    """
    try:
        probe = probe_env_contract(
            ctx["repo_name"], ref, verify_merged_into_main=verify_merged_into_main
        )
    except Exception as error:
        ctx.setdefault("env_contract_errors", {})[phase] = (
            f"{phase}: environment contract probe at {ref} could not run: "
            f"{type(error).__name__}: {error}"
        )
        return False

    ctx.setdefault("env_contract_probes", {})[phase] = probe
    error = _env_contract_failure(probe, phase, verify_merged_into_main)
    if error:
        ctx.setdefault("env_contract_errors", {})[phase] = error
        return False
    return True


# ── Deploy run outcome ───────────────────────────────────────────────────


def require_unscoped_run_observer(api_internal: httpx.AsyncClient) -> None:
    """Reject a run-observing client that authenticates as a user.

    list_runs narrows its result to ``Run.user_id == caller`` for every non-admin
    ``X-Telegram-ID`` it sees, and a valid internal key does not lift that
    narrowing. Deploy and QA producers can create runs with no user_id, so a
    user-scoped client is answered `[]` for them no matter which filter it passes.

    On 2026-07-16 that silently cost the mega a 420s wait for the already
    successful deploy `deploy-poll-ea0bed35`, so this is a loud crash rather than
    a blind poll.
    """
    if USER_AUTH_HEADER in api_internal.headers:
        raise RuntimeError(
            f"runs must be observed without {USER_AUTH_HEADER}: list_runs narrows "
            "its result to runs the non-admin harness user owns, while internal "
            "deploy and QA runs can have no user_id"
        )


async def wait_deploy_run(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: int = DEPLOY_RUN_TIMEOUT,
    poll_interval: float = DEPLOY_RUN_POLL_INTERVAL,
) -> dict | None:
    """Wait for this story's deploy run that carries the merged head SHA.

    pr_poller creates it only once the story PR reports merged_at, and records
    the merged head SHA in run_metadata — the exact ref deploy resolves the
    environment contract at. Engineering-triggered deploy runs carry no head_sha,
    so a run without one is not the run this mega deploys.

    The story is the link the API really supports for this: pr_poller stamps
    story_id on the run it creates, and a project can carry deploy runs of other
    stories. Both the filter and the returned run are checked against this mega's
    story, so a foreign run cannot be mistaken for it.

    Reads /api/runs/ as an internal service with no user header — see
    ``require_unscoped_run_observer``.
    """
    require_unscoped_run_observer(api_internal)
    story_id = ctx["story_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await api_internal.get(
            "/api/runs/",
            params={"story_id": story_id, "run_type": RunType.DEPLOY.value},
            headers=internal_headers(),
        )
        resp.raise_for_status()
        # The API orders runs newest first.
        for run in resp.json():
            if run["story_id"] != story_id:
                continue
            head_sha = (run["run_metadata"] or {}).get("head_sha")
            if head_sha:
                ctx["deploy_run_id"] = run["id"]
                ctx["deploy_head_sha"] = head_sha
                return run
        await asyncio.sleep(poll_interval)
    ctx["deploy_run_error"] = (
        f"no deploy run with a merged head_sha appeared for story {story_id} within {timeout}s"
    )
    return None


async def wait_deploy_outcome(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: int = DEPLOY_OUTCOME_TIMEOUT,
    poll_interval: float = DEPLOY_OUTCOME_POLL_INTERVAL,
) -> DeployRunResult | None:
    """Type this story's own deploy run result and record its outcome.

    A running application only proves some container answers; the deploy run
    result is what the pipeline itself concluded about the deploy, so the mega
    reads the typed outcome rather than trusting ApplicationStatus.
    """
    run_id = ctx["deploy_run_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await api_internal.get(f"/api/runs/{run_id}", headers=internal_headers())
        resp.raise_for_status()
        run = resp.json()
        if run["status"] in _TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(poll_interval)
    else:
        ctx["deploy_outcome_error"] = (
            f"deploy run {run_id} did not reach a terminal state in {timeout}s"
        )
        return None

    ctx["deploy_run_status"] = run["status"]
    if run["result"] is None:
        ctx["deploy_outcome_error"] = (
            f"deploy run {run_id} is {run['status']} but carries no result"
        )
        return None
    try:
        result = DeployRunResult(**run["result"])
    except ValidationError as error:
        ctx["deploy_outcome_error"] = (
            f"deploy run {run_id} result is not a DeployRunResult: {error}"
        )
        return None
    ctx["deploy_outcome"] = result.deploy_outcome.value
    ctx["deploy_error_details"] = result.error_details
    return result


# ── QA run outcome ───────────────────────────────────────────────────────


async def run_non_llm_qa(
    api_internal: httpx.AsyncClient,
    story_id: str,
    *,
    timeout: float,
    poll_interval: float = QA_RUN_POLL_INTERVAL,
) -> dict[str, str]:
    """Wait for this story's QA run and require a terminal ``passed``.

    The scheduler hands a successful deploy off to QA, and the QA consumer runs
    the repository's criteria — for a scaffolded project those are the seeded
    health check, which QA decides over HTTP with no LLM involved. The gate reads
    the run the pipeline produced: a health request issued by this test would
    prove the service answers, not that QA concluded anything about it.

    Reads /api/runs/ as an internal service with no user header — see
    ``require_unscoped_run_observer``.
    """
    require_unscoped_run_observer(api_internal)
    deadline = time.monotonic() + timeout
    run = None
    while time.monotonic() < deadline:
        resp = await api_internal.get(
            "/api/runs/",
            params={"story_id": story_id, "run_type": RunType.QA.value},
            headers=internal_headers(),
        )
        resp.raise_for_status()
        # The API orders runs newest first. A project can carry QA runs of other
        # stories, so the run is checked against this mega's story too.
        for candidate in resp.json():
            if candidate["story_id"] == story_id and candidate["status"] in _TERMINAL_RUN_STATUSES:
                run = candidate
                break
        if run is not None:
            break
        await asyncio.sleep(poll_interval)
    else:
        raise AssertionError(
            f"no QA run reached a terminal state for story {story_id} in {timeout}s"
        )

    result = run["result"] or {}
    outcome = result.get("qa_outcome")
    if run["status"] != "completed" or outcome != QAOutcome.PASSED.value:
        raise AssertionError(
            f"QA run {run['id']} ended with status={run['status']} outcome={outcome}: "
            f"{result.get('summary') or result.get('error')}"
        )
    return {"run_id": run["id"], "status": run["status"], "qa_outcome": outcome}


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


def cancel_and_wait_for_active_work(
    project_id: str,
    *,
    command=_redis_command,
    timeout: float = RUN_CANCELLATION_TIMEOUT,
    poll_interval: float = RUN_CANCELLATION_POLL_INTERVAL,
) -> None:
    """Fence capability consumers and wait until every owned execution lease has exited."""
    cancel_key = f"live:work:cancelled:{project_id}"
    leases_key = f"live:work:leases:{project_id}"
    failure_key = f"live:work:failed:{project_id}"
    command("SET", cancel_key, "1", "EX", str(SCAFFOLD_FENCE_TIMEOUT))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        failure = command("GET", failure_key)
        if failure:
            raise CleanupError(f"active work for project {project_id} could not settle: {failure}")
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
    raise CleanupError(f"active work for project {project_id} did not terminate")


def cancel_owned_active_work(ctx: dict) -> None:
    """Fence all capability consumers that can mutate this run's resources."""
    project_id = ctx.get("project_id")
    if project_id:
        cancel_and_wait_for_active_work(project_id)


def cleanup_owned_capability_work(ctx: dict) -> None:
    """Settle only this live run's queued and pending capability messages."""
    project_id = ctx.get("project_id")
    if not project_id:
        return
    identifiers = {
        resource.identifier
        for resource in ctx["manifest"].resources
        if resource.kind in {"run", "story"}
    }

    def record(message: CapabilityMessage) -> None:
        ctx["manifest"].own(
            "capability_message",
            f"{message.stream}/{message.message_id}",
            groups=list(message.groups),
        )
        ctx["manifest"].write(
            ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
        )

    cleanup_owned_capability_messages(
        project_id,
        identifiers,
        command=_redis_command,
        on_discovered=record,
    )


async def cancel_owned_runs(api_internal: httpx.AsyncClient, ctx: dict) -> list[str]:
    """Cancel every active run owned by this project before resource teardown."""
    require_unscoped_run_observer(api_internal)
    project_id = ctx.get("project_id")
    if not project_id:
        return []
    response = await api_internal.get("/api/runs/", params={"project_id": project_id})
    response.raise_for_status()
    run_ids = [
        str(run["id"])
        for run in response.json()
        if str(run.get("project_id")) == str(project_id)
        and run.get("status") in _ACTIVE_RUN_STATUSES
    ]
    for run_id in run_ids:
        response = await api_internal.patch(
            f"/api/runs/{run_id}",
            json={"status": "cancelled"},
        )
        response.raise_for_status()
        ctx["manifest"].own("run", run_id)
    if run_ids:
        ctx["manifest"].write(
            ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
        )
    return run_ids


async def wait_for_owned_runs(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = RUN_CANCELLATION_TIMEOUT,
    poll_interval: float = RUN_CANCELLATION_POLL_INTERVAL,
) -> None:
    """Wait until the run records owned by teardown are terminal."""
    require_unscoped_run_observer(api_internal)
    run_ids = {
        resource.identifier for resource in ctx["manifest"].resources if resource.kind == "run"
    }
    if not run_ids:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await api_internal.get("/api/runs/", params={"project_id": ctx["project_id"]})
        response.raise_for_status()
        statuses = {
            str(run["id"]): run.get("status")
            for run in response.json()
            if str(run.get("project_id")) == str(ctx["project_id"])
        }
        if all(statuses.get(run_id) in _TERMINAL_RUN_STATUSES for run_id in run_ids):
            return
        await asyncio.sleep(poll_interval)
    raise CleanupError(f"owned runs did not reach terminal state: {', '.join(sorted(run_ids))}")


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
        "            if manifest.status_code == 404:\n"
        "                continue\n"
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
        "        if verify.status_code == 404:\n"
        "            return\n"
        "        verify.raise_for_status()\n"
        "        live_tags = []\n"
        "        for tag in verify.json().get('tags') or []:\n"
        "            manifest = await client.get(\n"
        "                f'{base}/v2/{repository}/manifests/{tag}', headers=headers\n"
        "            )\n"
        "            if manifest.status_code == 404:\n"
        "                continue\n"
        "            manifest.raise_for_status()\n"
        "            live_tags.append(tag)\n"
        "        if live_tags:\n"
        "            raise RuntimeError(f'registry tags remain for {repository}: {live_tags}')\n"
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


def _wait_for_container_absence(
    container: str,
    *,
    timeout: float = WORKER_REMOVAL_TIMEOUT,
    poll_interval: float = WORKER_REMOVAL_POLL_INTERVAL,
) -> str | None:
    """Return None after confirmed absence, otherwise a safe failure reason."""
    deadline = time.monotonic() + timeout
    while True:
        verify = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=ORCHESTRATOR_ROOT,
        )
        if verify.returncode != 0:
            inspect_error = f"{verify.stderr}\n{verify.stdout}".lower()
            if any(marker in inspect_error for marker in ("no such container", "no such object")):
                return None
            return "Docker inspect failed"
        if time.monotonic() >= deadline:
            return "still exists after removal wait"
        time.sleep(poll_interval)


def cleanup_owned_workers(
    ctx: dict,
    errors: list[str],
    *,
    timeout: float = WORKER_REMOVAL_TIMEOUT,
    poll_interval: float = WORKER_REMOVAL_POLL_INTERVAL,
) -> None:
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
            removal_reason = None
            if removed.returncode != 0 and not any(
                marker in removed.stderr for marker in ("No such container", "already in progress")
            ):
                removal_reason = "Docker removal failed"
            absence_reason = _wait_for_container_absence(
                container,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            reason = removal_reason or absence_reason
            if reason:
                errors.append(f"worker {worker_id} container {container}: {reason}")
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
        else:
            remaining = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "redis",
                    "redis-cli",
                    "EXISTS",
                    *keys,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=ORCHESTRATOR_ROOT,
            )
            if remaining.returncode != 0 or remaining.stdout.strip() != "0":
                errors.append(f"worker {worker_id} Redis cleanup: keys remain or verify failed")
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


def _build_server_remote_cleanup_command(
    project_name: str, service_base: str = "/opt/services"
) -> str:
    project = shlex.quote(project_name)
    variant = shlex.quote(project_name.replace("-", "_"))
    base = shlex.quote(service_base.rstrip("/"))
    return f"""
set -eu
PROJECT_NAME={project}
ALT_PROJECT_NAME={variant}
SVC_DIR={base}/$PROJECT_NAME
PROJECTS=$(printf '%s\\n%s\\n' "$PROJECT_NAME" "$ALT_PROJECT_NAME" | awk 'NF && !seen[$0]++')
for project in $PROJECTS; do
  for c in $(docker ps -aq --filter "name=^/${{project}}[-_]"); do
    label=$(docker inspect -f '{{{{ index .Config.Labels "com.docker.compose.project" }}}}' "$c")
    if [ -n "$label" ] && [ "$label" != "<no value>" ]; then
      PROJECTS=$(printf '%s\\n%s\\n' "$PROJECTS" "$label" | awk 'NF && !seen[$0]++')
    fi
  done
done
if [ -d "$SVC_DIR/infra" ]; then
  for project in $PROJECTS; do
    (cd "$SVC_DIR/infra" && docker compose -p "$project" down --remove-orphans -v)
  done
fi
for project in $PROJECTS; do
  for c in $(docker ps -aq --filter "label=com.docker.compose.project=$project"); do
    docker rm -f "$c"
  done
  for c in $(docker ps -aq --filter "name=^/${{project}}[-_]"); do
    docker rm -f "$c"
  done
  for resource in volume network; do
    for id in $(docker "$resource" ls -q --filter "label=com.docker.compose.project=$project"); do
      docker "$resource" rm "$id"
    done
  done
done
rm -rf "$SVC_DIR"
remaining=
for project in $PROJECTS; do
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$project")
  if [ -n "$ids" ]; then
    remaining="$remaining label:$project:$ids"
  fi
  ids=$(docker ps -aq --filter "name=^/${{project}}[-_]")
  if [ -n "$ids" ]; then
    remaining="$remaining name:$project:$ids"
  fi
  for resource in volume network; do
    ids=$(docker "$resource" ls -q --filter "label=com.docker.compose.project=$project")
    if [ -n "$ids" ]; then
      remaining="$remaining $resource:$project:$ids"
    fi
  done
done
if [ -n "$remaining" ]; then
  echo "compose residue remains:$remaining" >&2
  exit 1
fi
test ! -e "$SVC_DIR"
""".strip()


def build_server_cleanup_script(project_name: str, server_ip: str, server_handle: str) -> str:
    """Build the container-side teardown of one deployed stack.

    Runs inside langgraph so it can reach the internal API. The server and
    ssh-key fetches authenticate with X-Internal-Key like the real consumers:
    /api/servers/* is gated by require_internal_or_admin and 401s without it.

    SSH runs as the server's configured ``ssh_user`` (read from the server DTO,
    the same user deploy authorizes), not a hardcoded ``root`` the orchestrator
    key is not authorized for.

    Remote steps mirror how deploy.yml creates resources:
    1. discover actual compose project labels from live containers
    2. docker compose down by manifest and discovered project names
    3. remove containers, volumes and networks by project label
    4. verify no project-owned Docker resource remains
    5. remove and verify `/opt/services/{name}`
    """
    remote_cmd = _build_server_remote_cleanup_command(project_name)

    return (
        "import asyncio, sys, os\n"
        "sys.path.insert(0, '/app')\n"
        "import structlog\n"
        "logger = structlog.get_logger()\n"
        "async def main():\n"
        "    import httpx\n"
        "    api_url = os.environ.get('API_URL', 'http://api:8000')\n"
        "    headers = {'X-Internal-Key': os.environ['INTERNAL_API_KEY']}\n"
        "    async with httpx.AsyncClient("
        "base_url=api_url, timeout=10, headers=headers) as client:\n"
        f"        srv = await client.get('/api/servers/{server_handle}')\n"
        "        if srv.status_code != 200:\n"
        "            raise RuntimeError(f'server fetch failed: {srv.status_code}')\n"
        "        ssh_user = srv.json()['ssh_user']\n"
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
        f"             ssh_user + '@{server_ip}',\n"
        f"             {repr(remote_cmd)}],\n"
        "            capture_output=True, text=True, timeout=60,\n"
        "        )\n"
        "        if result.returncode != 0:\n"
        "            raise RuntimeError(\n"
        "                f'cleanup ssh failed: {result.returncode} {result.stderr[:300]}'\n"
        "            )\n"
        "        else:\n"
        f"            logger.info('cleanup_server_done', "
        f"project='{project_name}', server='{server_ip}', ssh_user=ssh_user)\n"
        "    finally:\n"
        "        os.unlink(key_path)\n"
        "asyncio.run(main())\n"
    )


def cleanup_server_container(ctx: dict) -> None:
    """Stop and remove deployed container on remote server via SSH."""
    if "server_handle" not in ctx:
        return
    script = build_server_cleanup_script(
        ctx["project_name"], ctx.get("server_ip", "127.0.0.1"), ctx["server_handle"]
    )
    result = docker_exec("langgraph", script, timeout=75)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


async def cleanup_all(
    api_internal: httpx.AsyncClient,
    api_no_auth: httpx.AsyncClient | None,
    ctx: dict,
) -> None:
    """Delete owned resources using an unscoped internal run observer."""
    errors: list[str] = []

    # XDEL cannot cancel a claimed message. Fence the consumer and wait for any
    # active scaffold job before deleting or verifying external resources.
    try:
        require_unscoped_run_observer(api_internal)
        cancel_owned_scaffold(ctx)
        await cancel_owned_runs(api_internal, ctx)
        await wait_for_owned_runs(api_internal, ctx)
        cancel_owned_active_work(ctx)
        cleanup_owned_capability_work(ctx)
    except Exception as exc:
        errors.append(f"active work cancellation fence: {exc}")
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
            # /api/servers/{handle}/ports is gated by require_internal_or_admin, so
            # authenticate as an internal service like the real consumers, not the
            # header-less api_no_auth client which gets 401.
            ports = await api_no_auth.get(
                f"/api/servers/{ctx['server_handle']}/ports", headers=internal_headers()
            )
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
        verify = await api_internal.get(f"/api/projects/{ctx['project_id']}")
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
        # application_health_history FKs applications (NO ACTION), delete it first.
        f"DELETE FROM application_health_history WHERE application_id IN "
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
        f"- deploy_run_id: `{ctx.get('deploy_run_id')}`",
        f"- deploy_head_sha: `{ctx.get('deploy_head_sha')}`",
        f"- deploy_run_status: `{ctx.get('deploy_run_status')}`",
        f"- deploy_outcome: `{ctx.get('deploy_outcome')}`",
        f"- deploy_error_details: `{ctx.get('deploy_error_details')}`",
        f"- deploy_run_error: `{ctx.get('deploy_run_error')}`",
        f"- deploy_outcome_error: `{ctx.get('deploy_outcome_error')}`",
        "",
        "## Environment contract",
    ]
    probes = ctx.get("env_contract_probes") or {}
    if probes:
        for phase, probe in sorted(probes.items()):
            lines.extend(
                [
                    f"- {phase} @ `{probe['ref']}`",
                    f"  fragments: `{json.dumps(probe['fragment_paths'], sort_keys=True)}`",
                    f"  entries: `{json.dumps(probe['entries'], sort_keys=True)}`",
                    f"  merged_into_main: `{probe['merged_into_main']}`",
                ]
            )
    else:
        lines.append("- none captured")
    for phase, error in sorted((ctx.get("env_contract_errors") or {}).items()):
        lines.append(f"- {phase} FAILED: {error}")
    lines.extend(
        [
            "",
            "## CI failure evidence",
        ]
    )
    evidence = ctx.get("ci_failure_evidence") or []
    if evidence:
        for failure in evidence:
            lines.extend(
                [
                    f"- fix_task_id: `{failure['fix_task_id']}`",
                    f"  run_id: `{failure['run_id']}`",
                    f"  head_sha: `{failure['head_sha']}`",
                    f"  fingerprint: `{failure['fingerprint']}`",
                    f"  failed_jobs: `{json.dumps(failure['failed_jobs'], sort_keys=True)}`",
                ]
            )
    else:
        lines.append("- none captured")
    lines.extend([""])

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
