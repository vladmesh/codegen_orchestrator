"""Shared helpers for pipeline live tests.

Extracted from conftest.py so test modules can import them directly.
These are plain functions, not pytest fixtures.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from capability_cleanup import CapabilityMessage, cleanup_owned_capability_messages
import httpx
from live_harness import CleanupError, OwnershipManifest, cleanup_on_error, resolve_repo_root
from pydantic import ValidationError
import run_cleanup
from run_evidence import (
    LOG_TAIL_LINES,
    LOG_TAIL_MAX_CHARS,
    Capture,
    RunEvidenceCollector,
    WorkerRole,
    deploy_run_facts,
    engineering_run_facts,
    engineering_run_record,
    evidence_output_directory,
    qa_run_facts,
)

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetAdmissionRead,
    EngineeringBudgetReservationState,
)
from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorDecisionSource
from shared.contracts.dto.owner_notification import OwnerNotificationState
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunType
from shared.contracts.dto.run_result import DeployRunResult, EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.work_admission import WorkAdmissionOutcome, WorkAdmissionRead
from shared.contracts.queues.po import POSystemEvent
from shared.contracts.queues.qa import QAOutcome
from shared.contracts.service_ports import is_http_health_port_service
from shared.contracts.worker_evidence import secret_env_values
from shared.diagnostics import redact_diagnostic
from shared.live_contour import require_live_contour
from shared.live_harness_cleanup import (
    STORY_BRANCH_PROBE_MARKER,
    build_remote_cleanup_command,
)
from shared.queues import SCAFFOLD_QUEUE

# ── Constants ────────────────────────────────────────────────────────────
API_URL = "http://localhost:8000"
TEST_TELEGRAM_ID = 999_000_001
USER_AUTH_HEADER = "X-Telegram-ID"
AUTH_HEADERS = {USER_AUTH_HEADER: str(TEST_TELEGRAM_ID)}
INTERNAL_API_KEY_ENV = "INTERNAL_API_KEY"

GITHUB_ORG = "project-factory-organization"
TEMPLATE_REPO = "gh:vladmesh/service-template"
TEMPLATE_REF = "91e582180b4295bce45155759bdad0dfa43b75f3"
ORCHESTRATOR_ROOT = resolve_repo_root(Path(__file__))

# Timeouts (seconds)
SCAFFOLD_TIMEOUT = 120
ENGINEERING_TIMEOUT = 420  # 7 min (worker spawn + noop + CI)
LLM_ENGINEERING_TIMEOUT = 1800  # 30 min (worker spawn + LLM edits + CI-fix loop)
DEPLOY_TIMEOUT = 420  # 7 min (deploy.yml + smoke test)
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
# Completion is emitted after QA by the supervisor, then the durable owner
# notification is delivered to PO.  Undeploy runs over the same bounded deploy
# consumer path as a normal deployment, but has no GitHub workflow phase.
STORY_COMPLETION_TIMEOUT = 180
OWNER_NOTIFICATION_TIMEOUT = 180
UNDEPLOY_TIMEOUT = 300
LIFECYCLE_POLL_INTERVAL = 3
# One owned deploy's teardown: 60s of SSH per target server, plus container start.
SERVER_CLEANUP_TIMEOUT = 180
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
NOOP_FOLLOWUP_TASK_TITLE = "Noop follow-up integration task"
NOOP_FOLLOWUP_TASK_DESCRIPTION = "Second empty NoopRunner commit after the first task"

LLM_BACKEND_PROJECT_DESCRIPTION = (
    "Backend-only live LLM pipeline test. Build a minimal HTTP API that can deploy "
    "without any user-provided secrets."
)
# The field the paid suite asks the developer worker to add to the scaffolded
# health payload. The scaffold serves GET /health already, so a task phrased
# around the endpoint itself asks for nothing: the third paid run (33706516335)
# ended with an empty story branch because the worker correctly found the work
# already done. One field the template does not render is a change the worker
# has to write, and one the harness can see afterwards.
LLM_HEALTH_MARKER_FIELD = "e2e_marker"


def new_health_marker() -> str:
    """The marker value this run asks for, and only this run can be satisfied by.

    Minted per run, so no artifact left behind by an earlier run — a committed
    file, a cached image, a template that grew the field — can answer for this
    one. A run that shows the marker shows that this run's worker wrote it.
    """
    return f"e2e-{secrets.token_hex(6)}"


def llm_backend_detailed_spec(marker: str) -> str:
    """The project-level spec of the paid backend, carrying this run's marker."""
    return f"""Implement a backend-only service.

Requirements:
- Keep the project backend-only. Do not add frontend, Telegram, notification, or bot modules.
- Do not require any user-provided secrets or environment variables.
- Keep the existing deploy contract limited to generated, computed, or literal values.
- GET /health returns HTTP 200 and a JSON object that carries the field
  "{LLM_HEALTH_MARKER_FIELD}" with the exact string value "{marker}".
- Cover that field with one focused backend test.
- Run the repository's normal formatting, linting, and unit tests before committing.
"""


LLM_BACKEND_TASK_TITLE = "Add the run marker to the backend health payload"


def llm_backend_task_description(marker: str) -> str:
    """The engineering task the paid suite drives, in the shape the plan names."""
    return (
        "The scaffolded backend already serves GET /health, so this task is about what that "
        f'endpoint returns. Add the field "{LLM_HEALTH_MARKER_FIELD}" to its JSON response '
        f'with the exact string value "{marker}", keeping the response HTTP 200 and the rest '
        "of the payload as it is. Add or update one focused backend test asserting that "
        f'GET /health answers 200 and that its JSON carries "{LLM_HEALTH_MARKER_FIELD}" equal '
        f'to "{marker}". Keep the app backend-only and deployable with no user-required secrets.'
    )


def llm_qa_acceptance_criteria(marker: str) -> str:
    """What QA is told to observe: the field this run's task added, and nothing else.

    The transport contract only — the change is observable and the executor
    really ran. Architecture, diff size and style are not QA's to grade.
    """
    return f"""- GET /health returns HTTP 200.
- Inspect the JSON returned by GET /health and report the observed value of the field
  "{LLM_HEALTH_MARKER_FIELD}"; it must be exactly "{marker}".
"""


LIVE_WORKER_AGENT_TYPE_ENV = "LIVE_WORKER_AGENT_TYPE"
LIVE_LLM_QA_ENV = "LIVE_LLM_QA"
LIVE_MATRIX_AGENT_TYPES = frozenset({"claude", "codex"})


# ── Low-level helpers ────────────────────────────────────────────────────


def require_internal_api_key() -> str:
    """The internal key every harness client authenticates with, or a named error.

    Read once at session start (``pytest_sessionstart``) so a missing variable is
    a sentence naming it, not a ``KeyError`` raised from the first client an hour
    into a mega run.
    """
    if INTERNAL_API_KEY_ENV not in os.environ:
        raise RuntimeError(
            f"{INTERNAL_API_KEY_ENV} is not set in the environment. Every live-harness "
            "client authenticates with it — since the global auth gate landed, a request "
            f"carrying only {USER_AUTH_HEADER} is answered 401. Export "
            f"{INTERNAL_API_KEY_ENV} (it is in the stack's .env) before running live tests."
        )
    return os.environ[INTERNAL_API_KEY_ENV]


def internal_headers() -> dict[str, str]:
    """Auth headers for internal-service endpoints, as the real consumers send them.

    /api/servers/* is gated by require_internal_or_admin, and the harness user is
    not admin, so without this header those endpoints answer 401/403.

    This key alone does not make /api/runs/ show every run: list_runs still
    narrows its result to the caller's own runs whenever it sees a non-admin
    X-Telegram-ID. Unowned runs are only visible to a client that sends no user
    header at all — see ``require_unscoped_run_observer``.
    """
    return {"X-Internal-Key": require_internal_api_key()}


# ── API clients: the three kinds, each built in exactly one place ────────
#
# Every live client is one of these three, and the name of the factory says
# which. Nothing outside this section composes auth headers: a call site that
# assembles its own is how the mega ended up with a client that authenticated as
# nobody.


def api_client_as_test_user(**kwargs) -> httpx.AsyncClient:
    """Acts ON BEHALF OF the harness test user.

    Both credentials, and both are needed: the internal key is what gets past the
    global auth gate (``require_authenticated_caller``), while X-Telegram-ID is
    what the request is judged as (``resolve_actor``). The key does not deputize
    the named user — a request naming a non-admin user is still that non-admin
    user. This is the client for the product path: projects, stories, tasks.
    """
    return _api_client({**internal_headers(), **AUTH_HEADERS}, **kwargs)


def api_client_as_internal_service(**kwargs) -> httpx.AsyncClient:
    """Acts as an internal service, naming no user.

    Server, ssh-key and allocation endpoints are gated by require_internal_or_admin
    and reject the non-admin harness user, so they are reached exactly as the
    production consumers reach them.
    """
    return _api_client(internal_headers(), **kwargs)


def api_client_as_unscoped_observer(**kwargs) -> httpx.AsyncClient:
    """Observes runs and servers that belong to no user. Never carries a user header.

    Same wire credentials as ``api_client_as_internal_service`` today, but a
    stricter contract, and the difference is load-bearing: list_runs narrows its
    result to the caller's own runs the moment it sees a non-admin X-Telegram-ID,
    and a valid internal key does not lift that narrowing. Deploy and QA runs have
    no telegram_chat_id, so a user header here makes them invisible — the 2026-07-16
    blindness that hotfix #232 repaired. The invariant is asserted, not merely
    documented, so it cannot drift back in.
    """
    client = _api_client(internal_headers(), **kwargs)
    require_unscoped_run_observer(client)
    return client


def _api_client(headers: dict[str, str], **kwargs) -> httpx.AsyncClient:
    """One httpx client for the live stack; the caller's factory picks the identity."""
    kwargs.setdefault("base_url", API_URL)
    kwargs.setdefault("timeout", 10)
    return httpx.AsyncClient(headers=headers, **kwargs)


def docker_exec(service: str, script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a Python script inside a docker compose service."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service, "python", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ORCHESTRATOR_ROOT,
    )


def docker_exec_python_module(
    service: str, module: str, args: list[str], timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run a Python module inside a docker compose service."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service, "python", "-m", module, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=ORCHESTRATOR_ROOT,
    )


async def ensure_test_user(
    api: httpx.AsyncClient, api_internal: httpx.AsyncClient | None = None
) -> None:
    """Ensure the harness's fixture user exists, then touch it as that user.

    Registration is promo-gated: `_requires_promo` exempts only a service acting
    for itself, so a request naming `X-Telegram-ID` must redeem a code. The
    harness user is a fixture, not a customer walking through the product door,
    so when `api_internal` is given it is created by the internal service naming
    nobody. Updating an existing user needs no code, which is why the user-client
    call below still stands: it is what proves the product client's header
    composition is accepted by the auth gate.

    `api_internal` is optional so the header-composition contract test can drive
    this function with one fake transport and no server behind it.
    """
    if api_internal is not None:
        created = await api_internal.post(
            "/api/users/upsert",
            json={
                "telegram_id": TEST_TELEGRAM_ID,
                "username": "live_test_bot",
                "first_name": "Live",
                "last_name": "Test",
            },
        )
        created.raise_for_status()

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
    """Create project + repository for one live pipeline variant. Returns ctx dict.

    This factory serves the scaffold, engineering and full pipelines alike, so it
    cannot tell whether this run will reach a deploy — and it does not guess. The
    deploy stack is owned by ``own_deploy_ahead``, from the fact that decides it.
    """
    suffix = secrets.token_hex(4)
    project_title = f"{project_prefix}-{suffix}"
    project_id = str(uuid.uuid4())
    # This run's identity, minted before the project it will work on. It is
    # handed over once, at project creation, and the platform carries it from
    # there onto every worker this run causes.
    manifest = OwnershipManifest(run_id=f"live-{uuid.uuid4().hex[:12]}")
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
            "title": project_title,
            "initiating_run_id": manifest.run_id,
            "status": ProjectStatus.DRAFT,
            "config": config,
        },
    )
    resp.raise_for_status()
    assert resp.status_code == 201, f"Create project failed: {resp.text}"
    project = resp.json()
    project_name = project["slug"]

    manifest.own("project", project_id)
    ctx = {
        "project_id": project_id,
        "project_title": project_title,
        "project_name": project_name,
        "repo_name": project_name,
        "manifest": manifest,
        "agent_type": agent_type,
        "scaffold_task_description": task_description,
        "task_title": task_title,
        "task_description": task_description,
    }

    async with cleanup_on_error(lambda: cleanup_all(api_internal, None, ctx)):
        manifest.write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{manifest.run_id}.json")
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
        project_prefix=require_live_contour().pipeline,
        description=NOOP_PROJECT_DESCRIPTION,
        agent_type="noop",
        task_title=NOOP_TASK_TITLE,
        task_description=NOOP_TASK_DESCRIPTION,
    )


async def create_llm_backend_project(
    api: httpx.AsyncClient, api_internal: httpx.AsyncClient
) -> dict:
    """Create project + repository for the live LLM backend pipeline.

    The marker is minted here, once, and every string this run drives the LLM
    path with is derived from it: the spec, the engineering task and — when the
    run asks for an LLM QA executor — the acceptance criteria. They cannot drift
    apart, because there is only one value and one place it comes from.
    """
    marker = new_health_marker()
    ctx = await create_pipeline_project(
        api,
        api_internal,
        project_prefix=require_live_contour().llm_pipeline,
        description=LLM_BACKEND_PROJECT_DESCRIPTION,
        detailed_spec=llm_backend_detailed_spec(marker),
        agent_type=live_worker_agent_type(),
        task_title=LLM_BACKEND_TASK_TITLE,
        task_description=llm_backend_task_description(marker),
    )
    ctx["health_marker"] = marker
    ctx["qa_requires_executor"] = os.getenv(LIVE_LLM_QA_ENV) == "1"
    if ctx["qa_requires_executor"]:
        response = await api.patch(
            f"/api/repositories/{ctx['repo_id']}",
            json={"acceptance_criteria": llm_qa_acceptance_criteria(marker)},
        )
        response.raise_for_status()
    return ctx


def live_worker_agent_type() -> str:
    """Resolve the real developer used by a live matrix run."""
    agent_type = os.getenv(LIVE_WORKER_AGENT_TYPE_ENV, "claude").strip().lower()
    if agent_type not in LIVE_MATRIX_AGENT_TYPES:
        allowed = ", ".join(sorted(LIVE_MATRIX_AGENT_TYPES))
        raise RuntimeError(
            f"{LIVE_WORKER_AGENT_TYPE_ENV} must be one of {allowed}, got {agent_type!r}"
        )
    return agent_type


def configured_qa_executor() -> str:
    """Read the executor selected by the live qa-worker container."""
    command = (
        "from src.config.settings import get_settings; "
        "print(get_settings().qa_executor_agent_type.value)"
    )
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "qa-worker", "python", "-c", command],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cannot read qa-worker executor: {result.stderr.strip()}")
    agent_type = result.stdout.strip()
    if agent_type not in LIVE_MATRIX_AGENT_TYPES:
        raise RuntimeError(f"qa-worker reported unsupported executor {agent_type!r}")
    return agent_type


def trigger_scaffold(ctx: dict) -> None:
    """Publish scaffold message to Redis stream."""
    ctx["manifest"].own("github_repository", f"{GITHUB_ORG}/{ctx['repo_name']}")
    ctx["manifest"].own("registry_repository", f"{GITHUB_ORG}/{ctx['repo_name']}-backend")
    ctx["manifest"].write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json")
    msg = {
        "project_id": ctx["project_id"],
        "repository_id": ctx["repo_id"],
        "telegram_chat_id": "live-test",
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


def own_deploy_ahead(ctx: dict) -> None:
    """Own this run's deploy stack before the pipeline can create it.

    The pipeline — not this harness — decides when a deploy run starts, so the
    only way the manifest can never lag the live stack is to own the stack name
    before anything can create it. Both facts that name it are knowable here: the
    stack is the project slug, and the targets are whatever `/api/servers/` lists
    at teardown. `wait_deploy` later enriches this same record with the resolved
    server and port; owning again merges into it rather than adding a second
    record. Without this, any failure between `docker compose up` on the target
    and that enrichment orphans a running stack teardown never hears of.

    Writing to disk is part of taking ownership: a run whose process dies is torn
    down by `scripts/clean_live_tests.py` from the file, not from this object.
    """
    ctx["manifest"].own("server_deployment", ctx["project_name"])
    ctx["manifest"].write(ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json")


async def create_story_and_task(
    api: httpx.AsyncClient, ctx: dict, *, linear_noop_tasks: bool = False
) -> None:
    """Create an in-progress Story and one or two schedulable engineering Tasks.

    Creating the story is what makes this run able to deploy, so this is where the
    deploy stack is owned — derived, not declared at the call site. Once the
    story's tasks are done the scheduler opens a PR from `story/<id>`, and
    `pr_poller` turns the merge into a deploy run and a `DeployMessage` without
    asking the harness. Every live run that can reach a deploy comes through here
    (a deploy run is created from a merged story PR), and a run that never creates
    a story — scaffold — never reaches one. A new live test therefore inherits the
    safe outcome without knowing this rule exists: driving engineering at all
    means owning the stack.

    Ownership is taken before the story is posted, so even a crash mid-creation
    leaves teardown able to clear the stack.
    """
    own_deploy_ahead(ctx)

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

    async def create_task(
        *, title: str, description: str, blocked_by_task_id: str | None = None
    ) -> str:
        response = await api.post(
            "/api/tasks/",
            json={
                "project_id": ctx["project_id"],
                "story_id": ctx["story_id"],
                "type": "create",
                "title": title,
                "description": description,
                "status": TaskStatus.BACKLOG,
                "blocked_by_task_id": blocked_by_task_id,
            },
        )
        response.raise_for_status()
        assert response.status_code == 201, f"Create task failed: {response.text}"
        task_id = response.json()["id"]
        response = await api.post(
            f"/api/tasks/{task_id}/transition",
            params={"to_status": TaskStatus.TODO},
            json={"actor": "live-test"},
        )
        response.raise_for_status()
        assert response.status_code == 200, f"Task transition to todo failed: {response.text}"
        return task_id

    first_task_id = await create_task(
        title=ctx.get("task_title", NOOP_TASK_TITLE),
        description=ctx.get("task_description", NOOP_TASK_DESCRIPTION),
    )
    ctx["task_id"] = first_task_id
    ctx["first_task_id"] = first_task_id
    ctx["task_ids"] = [first_task_id]
    if linear_noop_tasks:
        second_task_id = await create_task(
            title=NOOP_FOLLOWUP_TASK_TITLE,
            description=NOOP_FOLLOWUP_TASK_DESCRIPTION,
            blocked_by_task_id=first_task_id,
        )
        ctx["second_task_id"] = second_task_id
        ctx["task_ids"].append(second_task_id)


async def wait_engineering(
    api: httpx.AsyncClient,
    ctx: dict,
    timeout: int = ENGINEERING_TIMEOUT,
    *,
    on_poll: Callable[[], None] | None = None,
) -> None:
    """Wait for engineering to complete. Updates ctx['task_status'], ctx['story_status'].

    ``on_poll`` runs once per poll, before the task status is read, and once
    before the first wait. It exists for evidence collection: worker-manager
    removes a dead worker's container when the next attempt for the same project
    starts, and a removed container is the one thing the run label cannot find
    afterwards. Everything a pass needs is on the container from creation, so a
    pass that lands any time before that removal is enough — but a pass that
    never runs sees nothing at all, which is why the first one precedes the
    first sleep.
    """
    done_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    status = None
    elapsed = 0
    if on_poll is not None:
        on_poll()
    while elapsed < timeout:
        await asyncio.sleep(5)
        elapsed += 5
        if on_poll is not None:
            on_poll()
        resp = await api.get(f"/api/tasks/{ctx['task_id']}")
        resp.raise_for_status()
        task = resp.json()
        _record_task_diagnostic(ctx, task, task_id=ctx["task_id"])
        status = task.get("status")
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


def _engineering_run_candidates(payload: list[dict], task_id: str) -> list[dict]:
    """Keep only the engineering attempts explicitly bound to one planning task."""
    return [
        run
        for run in payload
        if run.get("type") == RunType.ENGINEERING.value and run.get("task_id") == task_id
    ]


def redacted_payload(payload: object) -> object:
    """One control-plane payload, redacted before it becomes retained evidence.

    The same rule the worker log tails are held to: a serialized payload is
    passed through `shared.diagnostics.redact_diagnostic` against every value of
    this process's environment whose name says it is a secret. Nothing new is
    trusted and no new secret-handling path is introduced.
    """
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return json.loads(redact_diagnostic(serialized, secrets=secret_env_values(dict(os.environ))))


def record_qa_run(ctx: dict, run: dict) -> None:
    """Read the terminal QA Run into evidence, inside the wait that found it.

    The run this receives is the record the QA consumer wrote, so its blocker —
    the category, what QA attempted, and what came back — is already in hand and
    needs no second call to a stand that is about to be deleted. Only the outcome
    used to be kept, and `qa_outcome=blocked` names no cause at all: run
    33711527100 stopped on `DEPLOYED_URL_UNREACHABLE` 0.9 s after a deploy the
    same pipeline had just called a success, and the artifact could not say so.

    This is evidence collection, so it never fails the run: a record that could
    not be read is a stated missed capture.
    """
    ctx["qa_run"] = run
    try:
        ctx["qa_run_record"] = redacted_payload(qa_run_facts(run))
    except (AttributeError, TypeError, ValueError) as error:
        ctx["qa_run_record_error"] = (
            f"the terminal QA Run could not be read into evidence: {type(error).__name__}: {error}"
        )


def record_deploy_run(ctx: dict, run: dict) -> None:
    """Read the terminal deploy Run into evidence, beside the QA Run it fed.

    The smoke evidence that made this deploy a success is the counter-fact to a
    QA stage that could not reach the same URL, and it exists only in this Run's
    result. Recorded whatever the outcome was: a deploy that failed is read here
    too, so the deploy stage's own reason no longer depends on the typed outcome
    alone.
    """
    try:
        ctx["deploy_run_record"] = redacted_payload(deploy_run_facts(run))
    except (AttributeError, TypeError, ValueError) as error:
        ctx["deploy_run_record_error"] = (
            f"deploy run {run.get('id')} could not be read into evidence: "
            f"{type(error).__name__}: {error}"
        )


async def record_health_probe(ctx: dict, url: str, *, expect_marker: str | None = None) -> dict:
    """Probe the deployed URL from the orchestrator host and keep either answer.

    `probe_health_endpoint` raises when the URL does not answer, and that raise
    is the harness contract — it stays. What changes is that the failure stops
    being unnameable: the error is recorded before it propagates, so the artifact
    can say the orchestrator itself could not reach the deployment either,
    instead of leaving a blank where the third reachability read should be.
    """
    try:
        evidence = await probe_health_endpoint(url, expect_marker=expect_marker)
    except AssertionError as error:
        ctx["health_probe_error"] = str(error)
        raise
    ctx["health_probe_before_undeploy"] = evidence
    return evidence


async def _executor_diagnostics_snapshot(api_internal: httpx.AsyncClient) -> Capture:
    """The executor diagnostics the control plane held at this moment."""
    try:
        response = await api_internal.get("/api/work-admission/executor-diagnostics")
        response.raise_for_status()
    except httpx.HTTPError as error:
        return Capture.missed(
            f"the executor diagnostics snapshot could not be read: {type(error).__name__}: {error}"
        )
    return Capture.captured(redacted_payload(response.json()))


async def _paid_run_admission(api_internal: httpx.AsyncClient, run_id: str) -> Capture:
    """The immutable work-admission outcome that allowed one Run to exist."""
    try:
        response = await api_internal.get(f"/api/work-admission/paid-runs/{run_id}/admission")
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        return Capture.missed(
            f"the work-admission audit for {run_id} answered HTTP {error.response.status_code}"
        )
    except httpx.HTTPError as error:
        return Capture.missed(
            f"the work-admission audit for {run_id} could not be read: {type(error).__name__}"
        )
    return Capture.captured(redacted_payload(response.json()))


async def record_engineering_evidence(api_internal: httpx.AsyncClient, ctx: dict) -> list[dict]:
    """Read every engineering Run of this combination while the stand still exists.

    On failure exactly as on success, and inside the phase rather than after it:
    the Run record, its admission outcome and the executor diagnostics in force
    are database and Redis facts on a machine the workflow deletes minutes
    later. A run that did not read them here has nothing to read afterwards, and
    its artifact can then name no reason for the stage that stopped it — which is
    exactly what run 33683482667 could not do.

    This is evidence collection, so it never fails the run: a read that could not
    be made is recorded as a stated missed capture, and a discovery that failed
    is recorded as the reason the whole section is missed. `RuntimeError` is
    caught with the transport and shape errors because `_engineering_runs_for_task`
    goes through `require_unscoped_run_observer`, which raises it: a collector
    that can fail the very run it is diagnosing is the wrong shape to leave here,
    even while no current client reaches that branch.
    """
    ctx["engineering_evidence_error"] = None
    records: list[dict] = []
    try:
        diagnostics = await _executor_diagnostics_snapshot(api_internal)
        for task_id in ctx.get("task_ids") or [ctx["task_id"]]:
            for run in await _engineering_runs_for_task(api_internal, task_id):
                run_id = run["id"]
                records.append(
                    engineering_run_record(
                        run_id=run_id,
                        task_id=task_id,
                        run=Capture.captured(redacted_payload(engineering_run_facts(run))),
                        admission=await _paid_run_admission(api_internal, run_id),
                        executor_diagnostics=diagnostics,
                    )
                )
    except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as error:
        ctx["engineering_evidence_error"] = (
            f"the engineering Runs of this combination could not be listed: "
            f"{type(error).__name__}: {error}"
        )
    ctx["engineering_runs"] = records
    return records


def _record_task_diagnostic(ctx: dict, task: dict, *, task_id: str | None = None) -> None:
    """Keep bounded, redacted terminal clues beside worker evidence."""
    diagnostic_task_id = task.get("id") or task_id
    if diagnostic_task_id is None:
        return
    failure_metadata = task.get("failure_metadata")
    if failure_metadata is not None:
        serialized = json.dumps(failure_metadata, sort_keys=True, default=str)
        redacted = redact_diagnostic(
            serialized,
            secrets=secret_env_values(dict(os.environ)),
        )
        failure_metadata = json.loads(redacted)
    ctx.setdefault("task_diagnostics", {})[diagnostic_task_id] = {
        "status": task.get("status"),
        "current_iteration": task.get("current_iteration"),
        "max_iterations": task.get("max_iterations"),
        "blocked_by_task_id": task.get("blocked_by_task_id"),
        "last_event": task.get("last_event"),
        "failure_metadata": failure_metadata,
    }


async def _read_task_with_diagnostic(
    api: httpx.AsyncClient,
    ctx: dict,
    task_id: str,
) -> dict:
    response = await api.get(f"/api/tasks/{task_id}")
    response.raise_for_status()
    task = response.json()
    _record_task_diagnostic(ctx, task, task_id=task_id)
    return task


async def _engineering_runs_for_task(api_internal: httpx.AsyncClient, task_id: str) -> list[dict]:
    require_unscoped_run_observer(api_internal)
    response = await api_internal.get(
        "/api/runs/", params={"task_id": task_id, "run_type": RunType.ENGINEERING.value}
    )
    response.raise_for_status()
    return _engineering_run_candidates(response.json(), task_id)


def _capture_dispatch_decisions(ctx: dict, runs: list[dict]) -> None:
    """Snapshot paid decisions as soon as dispatch exposes their immutable Run."""
    snapshots = ctx.setdefault("engineering_dispatch_decisions", {})
    for run in runs:
        decision = (run.get("run_metadata") or {}).get("executor_decision")
        if decision is not None:
            snapshots.setdefault(run["id"], decision)


async def wait_linear_noop_engineering(
    api: httpx.AsyncClient,
    api_internal: httpx.AsyncClient,
    ctx: dict,
    timeout: int = ENGINEERING_TIMEOUT,
    *,
    on_poll: Callable[[], None] | None = None,
) -> None:
    """Drive two dependent noop Tasks while observing the scheduler's fence.

    Both Tasks are already ``todo``.  The only thing preventing the second
    dispatch is its persisted dependency, so seeing an engineering Run for it
    before the first Task reaches ``done`` is a product failure, not a harness
    ordering convention.
    """
    first_task_id = ctx["first_task_id"]
    second_task_id = ctx["second_task_id"]
    terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    elapsed = 0
    first_status = None
    second_status = None
    if on_poll is not None:
        on_poll()
    while elapsed < timeout:
        await asyncio.sleep(5)
        elapsed += 5
        if on_poll is not None:
            on_poll()
        first_task = await _read_task_with_diagnostic(api, ctx, first_task_id)
        first_status = first_task.get("status")
        second_task = await _read_task_with_diagnostic(api, ctx, second_task_id)
        second_status = second_task.get("status")
        story_response = await api.get(f"/api/stories/{ctx['story_id']}")
        story_response.raise_for_status()
        story = story_response.json()
        if second_status != TaskStatus.DONE and story.get("pr_number") is not None:
            ctx["noop_task_sequence_error"] = (
                f"Story {ctx['story_id']} recorded PR {story['pr_number']} before dependent "
                f"task {second_task_id} reached done (status={second_status})"
            )
            break
        first_runs = await _engineering_runs_for_task(api_internal, first_task_id)
        second_runs = await _engineering_runs_for_task(api_internal, second_task_id)
        _capture_dispatch_decisions(ctx, [*first_runs, *second_runs])
        if first_status != TaskStatus.DONE and second_runs:
            ctx["noop_task_sequence_error"] = (
                f"dependent task {second_task_id} created engineering run(s) "
                f"{[run['id'] for run in second_runs]} before {first_task_id} reached done "
                f"(status={first_status})"
            )
            break
        if first_status in terminal:
            break

    ctx["first_task_status"] = first_status
    ctx["second_task_status_before_first_terminal"] = second_status
    if first_status != TaskStatus.DONE:
        ctx["task_status"] = first_status
        ctx["engineering_elapsed"] = elapsed
        return

    while elapsed < timeout * 2:
        await asyncio.sleep(5)
        elapsed += 5
        if on_poll is not None:
            on_poll()
        second_task = await _read_task_with_diagnostic(api, ctx, second_task_id)
        second_status = second_task.get("status")
        second_runs = await _engineering_runs_for_task(api_internal, second_task_id)
        _capture_dispatch_decisions(ctx, second_runs)
        if second_status in terminal:
            break

    ctx["second_task_status"] = second_status
    ctx["task_status"] = second_status
    ctx["engineering_elapsed"] = elapsed
    if second_status == TaskStatus.DONE:
        for _ in range(20):
            await asyncio.sleep(3)
            response = await api.get(f"/api/stories/{ctx['story_id']}")
            response.raise_for_status()
            ctx["story_status"] = response.json().get("status")
            if ctx["story_status"] in {
                StoryStatus.PR_REVIEW,
                StoryStatus.DEPLOYING,
                StoryStatus.COMPLETED,
                StoryStatus.FAILED,
            }:
                break


def _noop_settlement_error(ctx: dict, message: str) -> dict[str, dict]:
    ctx["noop_settlement_error"] = message
    ctx["noop_settlement"] = {}
    return {}


async def record_noop_settlement_evidence(  # noqa: PLR0911 - specific durable-boundary diagnostics
    api_internal: httpx.AsyncClient, ctx: dict
) -> dict[str, dict]:
    """Read exactly the durable paid-work facts a completed noop route owns.

    The noop profile deliberately has no provider usage.  Its ledger must
    therefore say ``cost_source=unknown`` with every provider/cost field empty;
    a reservation may be absent from enforcement (``unlimited`` or
    ``not_enforced``) or terminal after settlement, but it may never remain an
    active hold once its engineering Run is terminal.
    """
    evidence: dict[str, dict] = {}
    try:
        for task_id in ctx["task_ids"]:
            candidates = await _engineering_runs_for_task(api_internal, task_id)
            if len(candidates) != 1:
                return _noop_settlement_error(
                    ctx,
                    f"task {task_id} has {len(candidates)} engineering runs; expected exactly one",
                )
            listed = candidates[0]
            run_id = listed["id"]
            first = await api_internal.get(f"/api/runs/{run_id}")
            first.raise_for_status()
            run = first.json()
            second = await api_internal.get(f"/api/runs/{run_id}")
            second.raise_for_status()
            if run != second.json():
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} changed between terminal reads"
                )
            if run.get("status") != "completed":
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} ended with status={run.get('status')}"
                )
            result = EngineeringRunResult.model_validate(run.get("result"))
            if result.engineering_status is not EngineeringStatus.DONE:
                return _noop_settlement_error(
                    ctx,
                    (
                        f"engineering run {run_id} carries "
                        f"engineering_status={result.engineering_status}"
                    ),
                )
            decision = ExecutorDecision.from_run_metadata(run.get("run_metadata"))
            if (
                decision.agent_type.value != "noop"
                or decision.source is not ExecutorDecisionSource.PROJECT_PIN
                or decision.attempt_kind is not RunType.ENGINEERING
            ):
                return _noop_settlement_error(
                    ctx,
                    (
                        f"engineering run {run_id} has non-noop decision "
                        f"{decision.model_dump(mode='json')}"
                    ),
                )
            dispatched = ctx.get("engineering_dispatch_decisions", {}).get(run_id)
            if dispatched is not None and dispatched != decision.model_dump(mode="json"):
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} changed executor decision after dispatch"
                )
            admission_response = await api_internal.get(
                f"/api/work-admission/paid-runs/{run_id}/admission"
            )
            admission_response.raise_for_status()
            admission = WorkAdmissionRead.model_validate(admission_response.json())
            if admission.outcome is not WorkAdmissionOutcome.ADMITTED:
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} lacks admitted audit evidence"
                )
            reservation_response = await api_internal.get(
                f"/api/engineering-budget-policies/admissions/{run_id}"
            )
            reservation_response.raise_for_status()
            reservation = EngineeringBudgetAdmissionRead.model_validate(reservation_response.json())
            if reservation.attempt_id != run_id:
                return _noop_settlement_error(
                    ctx, f"reservation for {run_id} reported attempt_id={reservation.attempt_id}"
                )
            if reservation.outcome in {
                EngineeringBudgetAdmissionOutcome.UNLIMITED,
                EngineeringBudgetAdmissionOutcome.NOT_ENFORCED,
            }:
                if (
                    reservation.reservation_state is not None
                    or reservation.active_held_microusd != 0
                ):
                    return _noop_settlement_error(
                        ctx, f"unenforced noop attempt {run_id} retained a budget hold"
                    )
            elif reservation.reservation_state not in {
                EngineeringBudgetReservationState.RELEASED,
                EngineeringBudgetReservationState.SETTLED,
                EngineeringBudgetReservationState.UNKNOWN_FINAL,
            }:
                return _noop_settlement_error(
                    ctx,
                    f"noop reservation {run_id} is not terminal: {reservation.reservation_state}",
                )
            ledger_response = await api_internal.get(
                "/api/runs/engineering-attempts", params={"run_id": run_id}
            )
            ledger_response.raise_for_status()
            ledger = ledger_response.json()
            if len(ledger) != 1:
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} has {len(ledger)} ledger rows; expected one"
                )
            row = ledger[0]
            required_empty = (
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_microusd",
            )
            if (
                row.get("run_id") != run_id
                or row.get("project_id") != ctx["project_id"]
                or row.get("story_id") != ctx["story_id"]
                or row.get("task_id") != task_id
                or row.get("role") != "engineering"
                or row.get("cost_source") != "unknown"
                or any(row.get(field) is not None for field in required_empty)
            ):
                return _noop_settlement_error(
                    ctx, f"engineering run {run_id} has non-canonical noop ledger evidence"
                )
            evidence[run_id] = {
                "task_id": task_id,
                "decision": decision.model_dump(mode="json"),
                "admission": admission.model_dump(mode="json"),
                "reservation": reservation.model_dump(mode="json"),
                "ledger": row,
            }
    except (httpx.HTTPError, ValidationError, ValueError) as error:
        return _noop_settlement_error(
            ctx, f"could not read noop settlement evidence: {type(error).__name__}: {error}"
        )
    ctx["noop_settlement_error"] = None
    ctx["noop_settlement"] = evidence
    return evidence


async def verify_linear_noop_story_completion(api: httpx.AsyncClient, ctx: dict) -> bool:
    """Prove the merge/deploy gate saw both deterministic Story Tasks complete."""
    statuses: dict[str, str | None] = {}
    for task_id in ctx["task_ids"]:
        response = await api.get(f"/api/tasks/{task_id}")
        response.raise_for_status()
        statuses[task_id] = response.json().get("status")
    ctx["linear_noop_task_statuses_before_deploy"] = statuses
    if any(status != TaskStatus.DONE for status in statuses.values()):
        ctx["linear_noop_completion_error"] = (
            f"deploy gate reached before all Story Tasks completed: {statuses}"
        )
        return False
    worker_ids = ctx["run_evidence"].worker_ids(WorkerRole.DEVELOPER)
    ctx["linear_noop_worker_ids"] = worker_ids
    if len(worker_ids) != 1:
        ctx["linear_noop_completion_error"] = (
            "two-task noop Story did not preserve one developer worker lifecycle: "
            f"observed worker ids={worker_ids}"
        )
        return False
    ctx["linear_noop_completion_error"] = None
    return True


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
    api_observer: httpx.AsyncClient,
    ctx: dict,
    timeout: int = DEPLOY_TIMEOUT,
) -> None:
    """Wait for deploy to complete. Updates ctx with deployment info.

    Polls Application status (via repositories) instead of project.service_status.

    Owning the stack again on entry costs nothing when the story already did it —
    `own` merges — and closes the gap for any future run that reaches a deploy
    without a story. Under uncertainty the harness owns: an over-owned record
    costs an SSH round trip, an unowned one costs a live stack nobody knows about.
    """
    own_deploy_ahead(ctx)

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
    # /api/servers/ and its ports need internal-service auth and no named user:
    # the internal key authenticates the caller but does not deputize the named
    # user, so a request carrying the test user's X-Telegram-ID resolves to that
    # non-admin user and gets 403. That is exactly the unscoped observer, which
    # carries the key by construction. raise_for_status keeps a non-200 loud
    # instead of iterating an error body and crashing with TypeError before the
    # deploy reaches the ownership manifest.
    resp = await api_observer.get("/api/servers/")
    resp.raise_for_status()
    for srv in resp.json():
        resp = await api_observer.get(f"/api/servers/{srv['handle']}/ports")
        resp.raise_for_status()
        for alloc in resp.json():
            if alloc.get("application_id") == application["id"]:
                if not is_http_health_port_service(alloc.get("service_name")):
                    continue
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


def parse_probe_payload(stdout: str, marker: str, *, subject: str) -> dict:
    """Read a marked probe payload out of the container's stdout."""
    for line in stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker) :])
    raise RuntimeError(f"{subject} printed no payload: {stdout[:300]}")


def parse_env_contract_probe(stdout: str) -> dict:
    """Read the probe payload out of the container's stdout."""
    return parse_probe_payload(
        stdout, ENV_CONTRACT_PROBE_MARKER, subject="environment contract probe"
    )


def parse_story_branch_probe(stdout: str) -> dict:
    """Read the story-branch comparison out of the container's stdout."""
    return parse_probe_payload(stdout, STORY_BRANCH_PROBE_MARKER, subject="story branch probe")


def probe_env_contract(repo_name: str, ref: str, *, verify_merged_into_main: bool = False) -> dict:
    """Read the committed environment contract of one repository ref."""
    args = [
        "env-contract-probe",
        "--owner",
        GITHUB_ORG,
        "--repo",
        repo_name,
        "--ref",
        ref,
        "--marker",
        ENV_CONTRACT_PROBE_MARKER,
    ]
    if verify_merged_into_main:
        args.append("--verify-merged-into-main")
    result = docker_exec_python_module("langgraph", "shared.live_harness_cleanup", args, timeout=60)
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


# ── Story branch ─────────────────────────────────────────────────────────


def story_branch_name(story_id: str) -> str:
    """The branch the pipeline commits a story's work to."""
    return f"story/{story_id}"


def probe_story_branch(repo_name: str, branch: str) -> dict:
    """Compare one story branch with main through the stand's GitHub App."""
    args = [
        "story-branch-probe",
        "--owner",
        GITHUB_ORG,
        "--repo",
        repo_name,
        "--branch",
        branch,
        "--marker",
        STORY_BRANCH_PROBE_MARKER,
    ]
    result = docker_exec_python_module("langgraph", "shared.live_harness_cleanup", args, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"story branch probe for {repo_name}@{branch} failed: {result.stderr or result.stdout}"
        )
    return parse_story_branch_probe(result.stdout)


def record_story_branch_ahead(ctx: dict) -> bool:
    """Whether this story's branch carries a commit of its own. Records why not.

    This is the assertion that stands between engineering and the deploy wait.
    Engineering has just reported its task done, so whether a commit exists is
    settled — and it is the earliest moment it is settled, because before that
    the worker may still be writing one. The scheduler's ``complete_stories``
    is already retrying the story PR by now, every 30 s, and GitHub answers it
    422 "No commits between main and <branch>" every time; nothing about that
    loop can make a commit appear. So an empty branch is reported here, in one
    GitHub comparison, instead of by a 420-second deploy wait for a Run that no
    merge can ever create.

    A probe that cannot run at all is recorded the same way a probe that answers
    "not ahead" is — the run stops either way, and the reason says which
    happened, rather than an exception losing the evidence artifact.
    """
    branch = story_branch_name(ctx["story_id"])
    ctx["story_branch"] = branch
    try:
        probe = probe_story_branch(ctx["repo_name"], branch)
    except Exception as error:
        ctx["story_branch_error"] = (
            f"the story branch {branch} could not be compared with main, so it is unknown "
            f"whether engineering committed anything: {type(error).__name__}: {error}"
        )
        return False
    ctx["story_branch_compare"] = probe
    if probe["ahead_by"] < 1:
        ctx["story_branch_error"] = (
            f"no commit was made for this story: {branch} is not ahead of main "
            f"(compare status {probe['status']}, ahead_by {probe['ahead_by']}). The story PR "
            "GitHub refuses with 422 'No commits between main and this branch' can never be "
            "opened, so no merge, and no deploy run, can follow."
        )
        return False
    return True


# ── Deploy run outcome ───────────────────────────────────────────────────


def require_unscoped_run_observer(api_internal: httpx.AsyncClient) -> None:
    """Reject a run-observing client that authenticates as a user.

    list_runs narrows its result to ``Run.telegram_chat_id == caller`` for every non-admin
    ``X-Telegram-ID`` it sees, and a valid internal key does not lift that
    narrowing. Deploy and QA producers can create runs with no telegram_chat_id, so a
    user-scoped client is answered `[]` for them no matter which filter it passes.

    On 2026-07-16 that silently cost the mega a 420s wait for the already
    successful deploy `deploy-poll-ea0bed35`, so this is a loud crash rather than
    a blind poll.
    """
    if USER_AUTH_HEADER in api_internal.headers:
        raise RuntimeError(
            f"runs must be observed without {USER_AUTH_HEADER}: list_runs narrows "
            "its result to runs the non-admin harness user owns, while internal "
            "deploy and QA runs can have no telegram_chat_id"
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
        resp = await api_internal.get(f"/api/runs/{run_id}")
        resp.raise_for_status()
        run = resp.json()
        if run["status"] in _TERMINAL_RUN_STATUSES:
            break
        await asyncio.sleep(poll_interval)
    else:
        ctx["deploy_outcome_error"] = (
            f"deploy run {run_id} did not reach a terminal state in {timeout}s"
        )
        ctx["deploy_run_record_error"] = (
            f"deploy run {run_id} was never read into evidence: it did not reach a terminal "
            f"state in {timeout}s, so the record carries no result to read"
        )
        return None

    ctx["deploy_run_status"] = run["status"]
    # Read before the result is typed, so a run whose result is absent or does
    # not validate still leaves its record in the artifact rather than only the
    # error that typing it raised.
    record_deploy_run(ctx, run)
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
    record: Callable[[dict], None] | None = None,
    on_poll: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Wait for this story's QA run and require a terminal ``passed``.

    The scheduler hands a successful deploy off to QA, and the QA consumer runs
    the repository's criteria — for a scaffolded project those are the seeded
    health check, which QA decides over HTTP with no LLM involved. The gate reads
    the run the pipeline produced: a health request issued by this test would
    prove the service answers, not that QA concluded anything about it.

    ``record`` receives the terminal QA run before it is judged. It is what lets
    run evidence report the QA cell as exercised — and with which outcome — even
    when the outcome is what fails this gate.

    ``on_poll`` runs once per poll, and once before the first wait. The QA
    client enqueues its executor's deletion in a ``finally`` block, before the
    QA consumer persists the terminal run this function waits for, so the
    executor's container can be *removed* — not merely dead — by the time this
    returns. This wait is the window a pass has to land in.

    Reads /api/runs/ as an internal service with no user header — see
    ``require_unscoped_run_observer``.
    """
    require_unscoped_run_observer(api_internal)
    deadline = time.monotonic() + timeout
    run = None
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        resp = await api_internal.get(
            "/api/runs/",
            params={"story_id": story_id, "run_type": RunType.QA.value},
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

    if record is not None:
        record(run)
    result = run["result"] or {}
    outcome = result.get("qa_outcome")
    if run["status"] != "completed" or outcome != QAOutcome.PASSED.value:
        raise AssertionError(
            f"QA run {run['id']} ended with status={run['status']} outcome={outcome}: "
            f"{result.get('summary') or result.get('error')}"
        )
    return {"run_id": run["id"], "status": run["status"], "qa_outcome": outcome}


# ── Completed-story and undeploy lifecycle ───────────────────────────────


def _redis_json(*args: str) -> object:
    """Run one JSON Redis command through the stand's own Redis container."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "redis", "redis-cli", "--json", *args],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=ORCHESTRATOR_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout or "null")


def _flat_redis_fields(fields: dict[str, str] | list[str]) -> dict[str, str]:
    """Normalize redis-cli's RESP3 object and RESP2 flat-array JSON forms."""
    if isinstance(fields, dict):
        return {str(key): str(value) for key, value in fields.items()}
    return dict(zip(fields[::2], fields[1::2], strict=True))


def po_input_cursor(*, command: Callable[..., object] = _redis_json) -> str:
    """Capture the last PO input id before this run can publish an event."""
    from shared.queues import PO_INPUT_QUEUE

    entries = command("XREVRANGE", PO_INPUT_QUEUE, "+", "-", "COUNT", "1")
    return str(entries[0][0]) if entries else "0-0"


def po_events_after(
    cursor: str, *, command: Callable[..., object] = _redis_json
) -> list[POSystemEvent]:
    """Return typed PO system events strictly newer than a captured cursor."""
    from shared.queues import PO_INPUT_QUEUE

    entries = command("XRANGE", PO_INPUT_QUEUE, f"({cursor}", "+")
    events: list[POSystemEvent] = []
    for _entry_id, fields in entries:
        flat_fields = _flat_redis_fields(fields)
        if flat_fields.get("type") == "system_event":
            events.append(POSystemEvent.model_validate(flat_fields))
    return events


async def wait_story_completed(
    api: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = STORY_COMPLETION_TIMEOUT,
    poll_interval: float = LIFECYCLE_POLL_INTERVAL,
) -> dict | None:
    """Wait for this story's terminal completed state and preserve diagnostics."""
    story_id = ctx["story_id"]
    deadline = time.monotonic() + timeout
    last_story = None
    while time.monotonic() < deadline:
        response = await api.get(f"/api/stories/{story_id}")
        response.raise_for_status()
        last_story = response.json()
        if last_story.get("status") == StoryStatus.COMPLETED.value:
            ctx["story_terminal"] = last_story
            return last_story
        await asyncio.sleep(poll_interval)
    ctx["story_terminal_error"] = (
        f"story {story_id} did not reach {StoryStatus.COMPLETED.value} in {timeout}s; "
        f"last_status={(last_story or {}).get('status')}"
    )
    return None


def _matching_completion_event(
    events: list[POSystemEvent], notification: dict, ctx: dict
) -> POSystemEvent | None:
    """Return the one new PO event that is the durable completion record."""
    # POSystemEvent always names a subject through task_id. A story-level
    # notification has no task in its durable record, so the producer uses the
    # story id as that subject.
    expected_subject = notification.get("task_id") or notification.get("story_id")
    matches = [
        event
        for event in events
        if event.event == "story_completed"
        and event.story_id == ctx["story_id"]
        and event.project_id == ctx["project_id"]
        and event.text == notification.get("text")
        and event.task_id == expected_subject
    ]
    return matches[0] if len(matches) == 1 else None


async def wait_owner_completion_notification(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = OWNER_NOTIFICATION_TIMEOUT,
    poll_interval: float = LIFECYCLE_POLL_INTERVAL,
    events_after: Callable[[str], list[POSystemEvent]] = po_events_after,
) -> tuple[dict, POSystemEvent] | None:
    """Prove the durable completion record was accepted by the PO input stream."""
    story_id = ctx["story_id"]
    cursor = ctx["po_input_cursor"]
    deadline = time.monotonic() + timeout
    last_state = None
    last_events = 0
    while time.monotonic() < deadline:
        response = await api_internal.get(f"/api/stories/{story_id}/owner-notification")
        response.raise_for_status()
        notification = response.json()
        last_state = notification.get("state")
        events = events_after(cursor)
        last_events = len(events)
        event = _matching_completion_event(events, notification, ctx)
        valid_notification = (
            notification.get("event") == "story_completed"
            and notification.get("story_id") == story_id
            and notification.get("project_id") == ctx["project_id"]
            and notification.get("terminal_status") == StoryStatus.COMPLETED.value
            and notification.get("state") == OwnerNotificationState.DELIVERED.value
            and notification.get("task_id") is None
            and ctx["deployed_url"] in notification.get("text", "")
        )
        if valid_notification and event is not None:
            ctx["owner_notification"] = notification
            ctx["owner_notification_po_event"] = event.model_dump(mode="json")
            return notification, event
        await asyncio.sleep(poll_interval)
    ctx["owner_notification_error"] = (
        f"story {story_id} completion notification was not delivered to PO in {timeout}s; "
        f"last_state={last_state} events_after_cursor={last_events}"
    )
    return None


async def wait_service_deployment(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = DEPLOY_OUTCOME_TIMEOUT,
    poll_interval: float = LIFECYCLE_POLL_INTERVAL,
) -> dict | None:
    """Select exactly one successful deployment for this application and SHA."""
    application_id = ctx["application_id"]
    project_id = ctx["project_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await api_internal.get(
            "/api/service-deployments/",
            params={"application_id": application_id, "project_id": project_id},
        )
        response.raise_for_status()
        deployments = [
            deployment
            for deployment in response.json()
            if str(deployment.get("application_id")) == str(application_id)
            and str(deployment.get("project_id")) == str(project_id)
        ]
        if len(deployments) > 1:
            ctx["service_deployment_error"] = (
                f"ambiguous deployments for application {application_id}, project {project_id}: "
                f"{[deployment.get('id') for deployment in deployments]}"
            )
            return None
        if len(deployments) == 1:
            deployment = deployments[0]
            if (
                deployment.get("result") == "success"
                and deployment.get("deployed_sha") == ctx["deploy_head_sha"]
            ):
                ctx["service_deployment"] = deployment
                return deployment
            ctx["service_deployment_error"] = (
                f"deployment {deployment.get('id')} for application {application_id} has "
                f"result={deployment.get('result')} deployed_sha={deployment.get('deployed_sha')}; "
                f"expected success and {ctx['deploy_head_sha']}"
            )
        await asyncio.sleep(poll_interval)
    ctx.setdefault(
        "service_deployment_error",
        f"no successful deployment for application {application_id}, project {project_id} "
        f"with deployed_sha={ctx['deploy_head_sha']} appeared in {timeout}s",
    )
    return None


async def probe_health_endpoint(
    url: str, *, attempts: int = 5, retry_delay: float = 5, expect_marker: str | None = None
) -> dict:
    """Probe the public health endpoint while the application is still running.

    ``expect_marker`` is the value this run asked engineering to put in the
    payload. It is judged here, against the whole response, because the retained
    body is a bounded slice of it and a marker past that bound would be
    unreadable afterwards. A run that asks for no marker records exactly what it
    always did.
    """
    last_error = None
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(1, attempts + 1):
            for path in ("/health", "/v1/health"):
                try:
                    response = await client.get(f"{url}{path}")
                except httpx.ConnectError as error:
                    last_error = f"{type(error).__name__}: {error}"
                    continue
                evidence = {
                    "url": url,
                    "endpoint": path,
                    "status_code": response.status_code,
                    "body": response.text[:200],
                    "attempt": attempt,
                }
                if expect_marker is not None:
                    evidence["marker_present"] = expect_marker in response.text
                if response.status_code == 200:
                    return evidence
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt < attempts:
                await asyncio.sleep(retry_delay)
    raise AssertionError(
        f"health endpoint not reachable at {url} after {attempts} attempts: {last_error}"
    )


async def request_undeploy(
    api: httpx.AsyncClient, api_internal: httpx.AsyncClient, ctx: dict
) -> dict:
    """Request undeploy after snapshotting every prior deploy Run."""
    require_unscoped_run_observer(api_internal)
    response = await api_internal.get(
        "/api/runs/", params={"project_id": ctx["project_id"], "run_type": RunType.DEPLOY.value}
    )
    response.raise_for_status()
    ctx["deploy_run_ids_before_undeploy"] = {run["id"] for run in response.json()}
    response = await api.post(
        f"/api/applications/{ctx['application_id']}/undeploy", json={"actor": "live-test"}
    )
    response.raise_for_status()
    application = response.json()
    ctx["undeploy_request"] = application
    if application.get("status") != ApplicationStatus.UNDEPLOYING.value:
        ctx["undeploy_request_error"] = (
            f"undeploy request for application {ctx['application_id']} returned "
            f"status={application.get('status')}"
        )
    return application


async def wait_undeploy_run(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = UNDEPLOY_TIMEOUT,
    poll_interval: float = LIFECYCLE_POLL_INTERVAL,
) -> dict | None:
    """Find the one post-request deploy Run bound to this application."""
    require_unscoped_run_observer(api_internal)
    before = ctx["deploy_run_ids_before_undeploy"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await api_internal.get(
            "/api/runs/", params={"project_id": ctx["project_id"], "run_type": RunType.DEPLOY.value}
        )
        response.raise_for_status()
        matches = [
            run
            for run in response.json()
            if run["id"] not in before
            and str((run.get("run_metadata") or {}).get("application_id"))
            == str(ctx["application_id"])
        ]
        if len(matches) > 1:
            ctx["undeploy_run_error"] = (
                f"ambiguous new undeploy runs for application {ctx['application_id']}: "
                f"{[run['id'] for run in matches]}"
            )
            return None
        if len(matches) == 1 and matches[0].get("status") in _TERMINAL_RUN_STATUSES:
            ctx["undeploy_run"] = matches[0]
            return matches[0]
        await asyncio.sleep(poll_interval)
    ctx["undeploy_run_error"] = (
        f"no terminal undeploy run for application {ctx['application_id']} appeared in {timeout}s"
    )
    return None


async def wait_application_not_deployed(
    api: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float = UNDEPLOY_TIMEOUT,
    poll_interval: float = LIFECYCLE_POLL_INTERVAL,
) -> dict | None:
    """Wait for the lifecycle target to report the product terminal status."""
    deadline = time.monotonic() + timeout
    last_application = None
    while time.monotonic() < deadline:
        response = await api.get(f"/api/applications/{ctx['application_id']}")
        response.raise_for_status()
        last_application = response.json()
        if last_application.get("status") == ApplicationStatus.NOT_DEPLOYED.value:
            ctx["application_after_undeploy"] = last_application
            return last_application
        await asyncio.sleep(poll_interval)
    ctx["application_after_undeploy_error"] = (
        f"application {ctx['application_id']} did not reach {ApplicationStatus.NOT_DEPLOYED.value} "
        f"in {timeout}s; last_status={(last_application or {}).get('status')}"
    )
    return None


async def verify_undeploy_residue(api_internal: httpx.AsyncClient, ctx: dict) -> dict | None:
    """Fail closed unless the exact pre-undeploy port allocation is gone."""
    response = await api_internal.get(f"/api/servers/{ctx['server_handle']}/ports")
    response.raise_for_status()
    owned = [
        allocation
        for allocation in response.json()
        if str(allocation.get("id")) == str(ctx["allocation_id"])
        or str(allocation.get("application_id")) == str(ctx["application_id"])
    ]
    residue = {
        "application_id": ctx["application_id"],
        "allocation_id": ctx["allocation_id"],
        "port_allocation_absent": not owned,
        "observed_allocations": owned,
    }
    ctx["undeploy_residue"] = residue
    if owned:
        ctx["undeploy_residue_error"] = (
            f"undeploy left owned port allocations for application {ctx['application_id']}: {owned}"
        )
        return None
    return residue


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


async def _cancel_active_project_runs(
    api_internal: httpx.AsyncClient, ctx: dict, project_id: str
) -> tuple[list[str], dict[str, str]]:
    """Cancel every currently active run of this project, one snapshot at a time.

    Returns the ids cancelled in this pass and the statuses the snapshot reported,
    so a caller can tell "nothing left to cancel" from "still not terminal".
    """
    response = await api_internal.get("/api/runs/", params={"project_id": project_id})
    response.raise_for_status()
    statuses = {
        str(run["id"]): run.get("status")
        for run in response.json()
        if str(run.get("project_id")) == str(project_id)
    }
    cancelled = []
    for run_id, status in statuses.items():
        if status not in _ACTIVE_RUN_STATUSES:
            continue
        response = await api_internal.patch(
            f"/api/runs/{run_id}",
            json={"status": "cancelled"},
        )
        response.raise_for_status()
        ctx["manifest"].own("run", run_id)
        cancelled.append(run_id)
    if cancelled:
        ctx["manifest"].write(
            ORCHESTRATOR_ROOT / ".live-manifests" / f"{ctx['manifest'].run_id}.json"
        )
    return cancelled, statuses


async def cancel_owned_runs(api_internal: httpx.AsyncClient, ctx: dict) -> list[str]:
    """Cancel every active run owned by this project before resource teardown."""
    require_unscoped_run_observer(api_internal)
    project_id = ctx.get("project_id")
    if not project_id:
        return []
    cancelled, _ = await _cancel_active_project_runs(api_internal, ctx, project_id)
    return cancelled


async def wait_for_owned_runs(
    api_internal: httpx.AsyncClient,
    ctx: dict,
    *,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> None:
    """Wait until every run of this project is terminal, rescanning for new ones.

    The first cancellation snapshot is stale by the time teardown reads it: a
    supervisor can still create a deploy or QA run for this project afterwards.
    Each poll rescans `/api/runs/`, cancels whatever became active, and takes the
    project ownership. Quiescence means one snapshot where no run of the project
    is active and every owned run is terminal.

    The bounds are read from the module at call time so a caller of `cleanup_all`
    cannot silently inherit a stale copy of them.
    """
    require_unscoped_run_observer(api_internal)
    if timeout is None:
        timeout = RUN_CANCELLATION_TIMEOUT
    if poll_interval is None:
        poll_interval = RUN_CANCELLATION_POLL_INTERVAL
    project_id = ctx.get("project_id")
    if not project_id:
        return
    deadline = time.monotonic() + timeout
    while True:
        _, statuses = await _cancel_active_project_runs(api_internal, ctx, project_id)
        owned = {
            resource.identifier for resource in ctx["manifest"].resources if resource.kind == "run"
        }
        # Everything cancelled in this pass was active in the same snapshot, so it
        # is already pending: one more clean scan has to confirm it went terminal.
        pending = {run_id for run_id in owned if statuses.get(run_id) not in _TERMINAL_RUN_STATUSES}
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise CleanupError(
                "owned runs did not reach terminal state: " + ", ".join(sorted(pending))
            )
        await asyncio.sleep(poll_interval)


def cleanup_github_repo(repo_name: str) -> None:
    """Delete and verify one GitHub repo via the container's org token."""
    result = docker_exec_python_module(
        "langgraph",
        "shared.live_harness_cleanup",
        ["github-cleanup", "--owner", GITHUB_ORG, "--repo", repo_name],
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)


def cleanup_registry_repository(repository: str) -> None:
    """Delete and verify registry artifacts recorded for one live run."""
    result = docker_exec_python_module(
        "langgraph",
        "shared.live_harness_cleanup",
        ["registry-cleanup", "--repository", repository],
        timeout=90,
    )
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


def evidence_pass(ctx: dict) -> None:
    """One run-evidence pass over the containers this run's label selects.

    The capture is the whole of it, and it needs nothing but the run id: every
    worker this run caused carries `com.codegen.run.id` from creation, so a pass
    reads a worker that is already dead exactly as well as one that is running.

    Ownership is refreshed alongside it, and only as a second source. Redis
    names a worker while worker-manager still holds it, and the manifest keeps
    that name afterwards — which is how a worker whose container was *removed*,
    the one case the label query cannot answer, still reaches the artifact as a
    stated missed capture instead of an omission that would read as "nothing
    ran". A failed refresh must not stop the capture, and neither may fail the
    run: this is evidence collection, not a gate.
    """
    collector = ctx["run_evidence"]
    try:
        capture_owned_workers(ctx)
    except Exception as exc:
        collector.note_error(f"worker ownership refresh failed: {exc}")
    collector.capture()


def evidence_accounting(ctx: dict, ops: run_cleanup.CleanupOps) -> set[str]:
    """The workers this run's evidence accounts for, established before removal.

    A run that collected evidence of its own — every matrix run does, and its
    artifact is written in the `finally` block that precedes teardown — is asked
    for its records. A run that collected none takes one capture pass here and
    retains it, because cleanup is about to remove the containers and the
    metadata that pass is the last chance to read. Capture before cleanup in
    both cases: the order this sprint established, and reversing it would
    destroy the attributability the labels were added for.

    The pass reads its ownership from the same retained `worker:meta` keys
    cleanup is about to consider, so a worker whose removal record could not be
    stored reaches the evidence as a stated missed capture — and only then is
    its name a thing cleanup may take away.

    Then every worker the run's own label still lists is checked against those
    records and, if a capture failed for it, written down as an explicit missed
    capture: removal is fenced by accounting, and a capture that merely failed
    would otherwise fence the teardown forever while naming the worker nowhere.
    The result is retained — merged, never replaced — so the accounting that
    authorises the removal outlives the removal in both cases.
    """
    run_id = ctx["manifest"].run_id
    collector = ctx.get("run_evidence")
    if collector is None:
        collector = RunEvidenceCollector(
            run_id=run_id, owned_workers=lambda: ops.meta_workers(run_id)
        )
        collector.capture()
    run_cleanup.account_listed_workers(collector, ops, run_id)
    run_cleanup.retain_evidence(
        collector,
        ORCHESTRATOR_ROOT / ".live-manifests" / "evidence" / f"{collector.run_id}.json",
    )
    return run_cleanup.accounted_workers(collector)


def cleanup_owned_workers(
    ctx: dict,
    errors: list[str],
    *,
    timeout: float = WORKER_REMOVAL_TIMEOUT,
    poll_interval: float = WORKER_REMOVAL_POLL_INTERVAL,
    ops: run_cleanup.CleanupOps | None = None,
) -> None:
    """Remove everything this run's ownership label selects, and prove it is gone.

    Driven by `com.codegen.run.id`, not by what the manifest happens to still
    remember: a worker container this run caused, the QA-egress proxy beside it
    and the `dev_proj_<worker_id>` network under it are all stamped with this run
    at creation, so they are found and removed whether or not Redis still knows
    them and whether or not anything recorded them while they lived.

    The manifest is still refreshed first — it is the ownership source the run's
    evidence reconciles against — but it no longer decides what is removed.
    """
    try:
        capture_owned_workers(ctx)
    except Exception as exc:
        errors.append(f"worker ownership discovery: {exc}")
    cleanup_ops = (
        ops
        if ops is not None
        else run_cleanup.docker_cli_ops(
            ORCHESTRATOR_ROOT, timeout=timeout, poll_interval=poll_interval
        )
    )
    try:
        accounted = evidence_accounting(ctx, cleanup_ops)
    except Exception as exc:
        # Nothing is accounted for, so nothing that names a worker is deleted.
        errors.append(f"run evidence accounting: {exc}")
        accounted = set()
    try:
        report = run_cleanup.clean_run(
            cleanup_ops, ctx["manifest"].run_id, accounted_workers=accounted
        )
    except run_cleanup.RunCleanupError as exc:
        errors.append(str(exc))
        return
    ctx["run_cleanup"] = report.as_dict()
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
    return build_remote_cleanup_command(project_name, service_base)


def _server_cleanup_args(project_name: str, server_handle: str | None) -> list[str]:
    """Build the container-side teardown command for one owned deploy.

    Runs inside langgraph so it can reach the internal API. The server and
    ssh-key fetches authenticate with X-Internal-Key like the real consumers:
    /api/servers/* is gated by require_internal_or_admin and 401s without it.

    SSH runs as the server's configured ``ssh_user`` at the DTO's ``public_ip``
    (the same user and host deploy authorizes), not a hardcoded ``root`` the
    orchestrator key is not authorized for.

    ``server_handle`` is the resolved target once wait_deploy has read the
    allocation. A write-ahead deploy record has no target yet, and then teardown
    clears this stack name on every server the API lists.

    Remote steps mirror how deploy.yml creates resources:
    1. discover actual compose project labels from live containers
    2. docker compose down by manifest and discovered project names
    3. remove containers, volumes and networks by project label
    4. verify no project-owned Docker resource remains
    5. remove and verify `/opt/services/{name}`
    """
    args = [
        "server-cleanup",
        "--project-name",
        project_name,
        "--api-url",
        "http://api:8000",
    ]
    if server_handle is not None:
        args += ["--server-handle", server_handle]
    return args


def cleanup_server_container(ctx: dict) -> None:
    """Remove every deployed stack this run owns, reading only its manifest.

    One run's teardown is manifest-driven by contract: it removes the stack names
    this run wrote ahead, never everything that looks like a live-test stack.
    (The prefix sweep belongs to the global `make test-live-clean`, which owns no
    manifest.) A record enriched by wait_deploy names its target; a write-ahead
    record does not, and the cleanup module then resolves the targets itself.
    """
    for resource in ctx["manifest"].resources:
        if resource.kind != "server_deployment":
            continue
        metadata = resource.metadata
        handle = metadata["server_handle"] if "server_handle" in metadata else None
        result = docker_exec_python_module(
            "langgraph",
            "shared.live_harness_cleanup",
            _server_cleanup_args(resource.identifier, handle),
            # A record without a resolved target clears the stack on every listed
            # server, each with its own 60s SSH budget, so this is not the
            # single-target 75s any more.
            timeout=SERVER_CLEANUP_TIMEOUT,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)


async def fence_owned_work(api_internal: httpx.AsyncClient, ctx: dict) -> None:
    """Stop this run from producing or using any more resources.

    The first phase of cleaning a run, and a precondition of the three that
    follow it (capture, remove, verify). Everything that can still create a
    container, a network or a Redis key for this project is cancelled here and
    then waited out, so that what a later capture reads is what the run ended
    with rather than a moving target, and so that nothing is removed out from
    under work that is still running.

    It raises rather than returning a verdict: a caller that could not establish
    the fence has no business removing anything, and must say so loudly.
    """
    # XDEL cannot cancel a claimed message. Fence the consumer and wait for any
    # active scaffold job before deleting or verifying external resources.
    require_unscoped_run_observer(api_internal)
    cancel_owned_scaffold(ctx)
    await cancel_owned_runs(api_internal, ctx)
    await wait_for_owned_runs(api_internal, ctx)
    cancel_owned_active_work(ctx)
    cleanup_owned_capability_work(ctx)
    # The consumer fence is what stops new runs from being produced, so prove
    # run quiescence once more behind it: a run created while the first pass
    # was still waiting would otherwise survive into external teardown.
    await wait_for_owned_runs(api_internal, ctx)


async def cleanup_all(
    api_internal: httpx.AsyncClient,
    api_observer: httpx.AsyncClient | None,
    ctx: dict,
) -> None:
    """Delete owned resources using an unscoped internal run observer."""
    errors: list[str] = []

    try:
        await fence_owned_work(api_internal, ctx)
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
    if "allocation_id" in ctx and api_observer:
        try:
            resp = await api_observer.delete(f"/api/allocations/{ctx['allocation_id']}")
            if resp.status_code not in (200, 204, 404):
                raise RuntimeError(f"delete returned {resp.status_code}")
            # /api/servers/{handle}/ports is gated by require_internal_or_admin, so
            # this too goes through the unscoped observer, which carries the
            # internal key and names no user.
            ports = await api_observer.get(f"/api/servers/{ctx['server_handle']}/ports")
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

# The service tails are a coarser sample than a worker's own stdout, so they
# keep their tighter bound; the worker tail uses the shared `run_evidence`
# bounds, because it is the same kind of thing read the same way.
DEBUG_DUMP_SERVICE_TAIL_LINES = 30
DEBUG_DUMP_SERVICE_TAIL_MAX_CHARS = 2_000


def redacted_dump_text(text: str) -> str:
    """The assembled debug dump, redacted before it can be written down.

    The dump embeds container stdout, and a worker's stdout is not a trusted
    surface: `services/worker-manager/src/git_ops.py` gives the worker an origin
    of `https://x-access-token:<token>@github.com/...`, so an ordinary git
    failure prints a usable credential. The dump now crosses the handoff into an
    uploaded artifact, so it is held to the rule `redacted_payload` and the
    worker log tails already follow — `shared.diagnostics.redact_diagnostic`
    against every value of this process's environment whose name says it is a
    secret. No new secret-handling path: the same mechanism, applied one place
    further.
    """
    return redact_diagnostic(text, secrets=secret_env_values(dict(os.environ)))


def dump_debug(ctx: dict, test_name: str) -> None:
    """Write this run's own debug dump where the handoff can still collect it.

    Beside the run-evidence artifact, not under the checkout. On the stand the
    checkout is the ephemeral host: a dump written there dies with the machine
    and never reaches the acceptance artifact, which is how run 33683482667
    ended with no post-mortem at all. `evidence_output_directory` resolves the
    runner-owned directory the workflow collects from, and falls back to
    `docs/e2e_results/` for a local run.

    The file's name is recorded on the context, so the artifact names the dumps
    this run wrote rather than leaving a reader to guess whether any exist.

    Because it now crosses the handoff, the embedded log slices are bounded and
    the assembled text is redacted by `redacted_dump_text` before the write.
    """
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    directory = evidence_output_directory(ORCHESTRATOR_ROOT)
    filepath = os.path.join(directory, f"debug-{test_name}-{ts}.md")
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    ctx.setdefault("debug_dumps", []).append(os.path.basename(filepath))

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
        f"- story_branch: `{ctx.get('story_branch')}`",
        f"- story_branch_compare: `{json.dumps(ctx.get('story_branch_compare'), sort_keys=True)}`",
        f"- story_branch_error: `{ctx.get('story_branch_error')}`",
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

    # Dynamic developer containers are removed by cleanup immediately after the
    # fixture resumes. Capture their final stdout while ownership can still be
    # resolved, otherwise an exit_code is all the post-mortem retains.
    if ctx.get("manifest") is not None:
        try:
            capture_owned_workers(ctx)
            for resource in ctx["manifest"].resources:
                if resource.kind != "worker":
                    continue
                container = resource.metadata.get("container") or find_worker_container(
                    resource.identifier
                )
                if not container:
                    continue
                result = subprocess.run(
                    ["docker", "logs", f"--tail={LOG_TAIL_LINES}", container],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=ORCHESTRATOR_ROOT,
                )
                output = "\n".join(part for part in (result.stdout, result.stderr) if part.strip())
                if output:
                    lines.extend(
                        [
                            f"## dynamic worker {resource.identifier} logs (last {LOG_TAIL_LINES})",
                            "```",
                            output.strip()[-LOG_TAIL_MAX_CHARS:],
                            "```",
                            "",
                        ]
                    )
        except Exception as error:
            lines.extend([f"- dynamic worker log capture failed: {type(error).__name__}", ""])

    # Collect docker logs from relevant services
    for service in ["scaffolder", "engineering-worker", "scheduler", "deploy-worker"]:
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", f"--tail={DEBUG_DUMP_SERVICE_TAIL_LINES}", service],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=ORCHESTRATOR_ROOT,
            )
            if result.stdout.strip():
                lines.extend(
                    [
                        f"## {service} logs (last {DEBUG_DUMP_SERVICE_TAIL_LINES})",
                        "```",
                        result.stdout.strip()[-DEBUG_DUMP_SERVICE_TAIL_MAX_CHARS:],
                        "```",
                        "",
                    ]
                )
        except Exception:
            pass

    with open(filepath, "w") as f:
        f.write(redacted_dump_text("\n".join(lines)))
