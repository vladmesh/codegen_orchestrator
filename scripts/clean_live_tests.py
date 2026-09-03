# ruff: noqa: S608
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

CLEANUP_API_URL = "http://localhost:8000"
HTTP_OK = 200
ORCHESTRATOR_ROOT = os.environ.get("ORCHESTRATOR_ROOT")
if not ORCHESTRATOR_ROOT:
    ORCHESTRATOR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ORCHESTRATOR_ROOT not in sys.path:
    sys.path.insert(0, ORCHESTRATOR_ROOT)
from shared.live_contour import current_contour  # noqa: E402
from shared.live_harness_cleanup import (  # noqa: E402
    REMOTE_CLEANUP_SCRIPT,
    build_remote_cleanup_command,
    build_remote_residue_command,
    managed_cleanup_targets,
    tolerant_prefix_pattern,
    validate_managed_cleanup_target,
)

# One sweep, one contour. The prefixes and the organization both come from the
# contour the process runs in, so a stand sweep can never address production's
# projects — it does not know their names.
CONTOUR = current_contour()
PROJECT_PREFIXES = CONTOUR.project_prefixes
GITHUB_ORG = os.environ.get("GITHUB_ORG", "project-factory-organization")
# Deployed stacks are named by project slug, which truncates the title before the
# project UUID, so `live-test-…` projects deploy as `live-te-…` directories and
# containers. This is the only name an orphan still has once its DB rows are gone.
DEPLOY_SLUG_PREFIXES = CONTOUR.slug_prefixes
# Same dash-or-underscore rule the remote scan matches with: one implementation,
# so what the target reports and what this recognises cannot drift apart.
_STACK_NAME_PATTERN = re.compile(
    "^("
    + "|".join(tolerant_prefix_pattern(prefix) for prefix in DEPLOY_SLUG_PREFIXES)
    + ")[0-9a-f]{32}"
)


class CleanupFailure(RuntimeError):
    """Live cleanup could not prove that all test-owned resources are absent."""


def print_step(msg):
    print(f"\n\033[1;34m=== {msg} ===\033[0m")


def run_cmd(cmd, **kwargs):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=ORCHESTRATOR_ROOT,
        **kwargs,
    )


def _query_scalar(sql):
    result = run_cmd(
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
            "-t",
            "-A",
            "-c",
            sql,
        ]
    )
    if result.returncode != 0:
        raise CleanupFailure(f"residue query failed: {result.stderr.strip()}")
    return int(result.stdout.strip() or "0")


def collect_residue_state(
    project_ids: list[str] | None = None,
    *,
    remote_residue: dict[str, list[str]],
):
    """Return live-test residue counts using current schema relationships.

    ``remote_residue`` is what the targets reported (see ``collect_remote_residue``)
    and is counted here so a live stack on a deploy target cannot be missing from
    a residue verdict built only from the database, registry and Redis.
    """
    conditions = _build_conditions()
    manifests = list((Path(ORCHESTRATOR_ROOT) / ".live-manifests").glob("*.json"))
    projects = _query_scalar(f"SELECT count(*) FROM projects WHERE {conditions};")  # noqa: S608
    allocation_sql = (  # noqa: S608
        "SELECT count(*) FROM port_allocations pa "
        "JOIN applications a ON a.id = pa.application_id "
        "JOIN repositories r ON r.id = a.repo_id "
        f"JOIN projects p ON p.id = r.project_id WHERE {_build_conditions('p')};"  # noqa: S608
    )  # noqa: S608
    allocations = _query_scalar(allocation_sql)
    worker_scan = run_cmd(
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
        ]
    )
    if worker_scan.returncode != 0:
        raise CleanupFailure(f"worker residue scan failed: {worker_scan.stderr.strip()}")
    workers = len([line for line in worker_scan.stdout.splitlines() if line.strip()])
    residue = {
        "projects": projects,
        "allocations": allocations,
        "ownership_manifests": len(manifests),
        "workers": workers,
        "deployed_stacks": sum(len(found) for found in remote_residue.values()),
    }
    if project_ids:
        live_path = str(Path(ORCHESTRATOR_ROOT) / "tests" / "live")
        if live_path not in sys.path:
            sys.path.insert(0, live_path)
        from capability_cleanup import find_owned_capability_messages

        def command(*args):
            result = run_cmd(["docker", "compose", "exec", "-T", "redis", "redis-cli", *args])
            if result.returncode != 0:
                raise CleanupFailure(
                    f"capability stream verification failed: {result.stderr.strip()}"
                )
            return result.stdout.strip()

        residue["capability_messages"] = sum(
            len(find_owned_capability_messages(project_id, set(), command=command))
            for project_id in project_ids
        )
    return residue


def verify_no_residue(project_ids: list[str] | None = None):
    remote_residue = collect_remote_residue()
    residue = collect_residue_state(project_ids, remote_residue=remote_residue)
    remaining = {kind: count for kind, count in residue.items() if count}
    if remaining:
        details = ", ".join(f"{kind}={count}" for kind, count in sorted(remaining.items()))
        # Name what is still on the targets: an operator cannot act on a count.
        found = "; ".join(
            f"{handle}: {', '.join(sorted(items))}"
            for handle, items in sorted(remote_residue.items())
        )
        if found:
            details += f" ({found})"
        raise CleanupFailure(f"live-test residue remains: {details}")


def _build_conditions(alias: str | None = None):
    column = f"{alias}.title" if alias else "title"
    return " OR ".join([f"{column} LIKE '{p}-%'" for p in PROJECT_PREFIXES])


def manifest_project_ids() -> set[str]:
    """Keep manifest ownership available even if a prior crash already deleted DB rows."""
    project_ids: set[str] = set()
    for path in (Path(ORCHESTRATOR_ROOT) / ".live-manifests").glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CleanupFailure(f"invalid ownership manifest: {path.name}") from exc
        project_ids.update(
            str(resource["identifier"])
            for resource in data.get("resources", [])
            if resource.get("kind") == "project"
        )
    return project_ids


def _live_helpers_path() -> None:
    live_path = str(Path(ORCHESTRATOR_ROOT) / "tests" / "live")
    if live_path not in sys.path:
        sys.path.insert(0, live_path)


def internal_api_client():
    """The client every recovery phase talks to the live API with.

    `cleanup_all` and the fence in front of it cancel runs via `/api/runs/`,
    which rejects unauthenticated callers, so recovery authenticates as an
    internal service exactly as the live harness does.
    """
    import httpx

    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    return httpx.AsyncClient(base_url=CLEANUP_API_URL, timeout=20, headers=headers)


def manifest_context(data: dict) -> dict:
    """Rebuild the harness context one persisted manifest describes."""
    _live_helpers_path()
    from live_harness import OwnedResource, OwnershipManifest

    manifest = OwnershipManifest(
        run_id=str(data["run_id"]),
        resources=[
            OwnedResource(
                item["kind"],
                str(item["identifier"]),
                {key: value for key, value in item.items() if key not in {"kind", "identifier"}},
            )
            for item in data.get("resources", [])
        ],
    )
    ctx = {"manifest": manifest}
    for resource in manifest.resources:
        if resource.kind == "project":
            ctx["project_id"] = resource.identifier
        elif resource.kind == "github_repository":
            ctx["repo_name"] = resource.identifier.rsplit("/", 1)[-1]
        elif resource.kind == "port_allocation":
            ctx["allocation_id"] = resource.identifier
        elif resource.kind == "server_deployment":
            ctx["project_name"] = resource.identifier
            ctx.update(resource.metadata)
    return ctx


def fence_run_work(ctx: dict) -> list[str]:
    """Phase one of recovery: stop the run before anything reads or removes it.

    Losing the harness does not stop the run. The API, the scheduler and
    worker-manager keep going, so a worker carrying this run's label can still
    be running when an operator recovers the crashed harness — and a label sweep
    in front of the fence would capture that worker, account it and force-remove
    it while its run is still live. So recovery cancels the run's work and waits
    for quiescence first, using the fence the harness itself uses
    (`pipeline_helpers.fence_owned_work`) rather than a second one that could
    drift from it.

    A fence that cannot be established is reported, and its caller then removes
    nothing for this run: refusing is the correct answer only there.
    """
    import asyncio

    _live_helpers_path()
    import pipeline_helpers

    async def fence() -> None:
        async with internal_api_client() as api:
            await pipeline_helpers.fence_owned_work(api, ctx)

    try:
        asyncio.run(fence())
    except Exception as exc:
        return [f"run {ctx['manifest'].run_id} fence: {type(exc).__name__}: {exc}"]
    return []


def cleanup_manifest_resources(ctx: dict) -> list[str]:
    """Resume the same fail-closed owned cleanup used by the live harness."""
    import asyncio

    _live_helpers_path()
    from pipeline_helpers import cleanup_all

    async def resume() -> None:
        async with internal_api_client() as api:
            await cleanup_all(api, api, ctx)

    try:
        asyncio.run(resume())
    except Exception as exc:
        return [str(exc)]
    return []


def cleanup_run_scoped_resources(run_id: str) -> list[str]:
    """Remove every Docker resource this run's ownership label selects.

    Recovery's capture-and-remove phases, and the ones that need nothing but a
    run id: the containers, QA-egress sidecars and dev networks this run created
    carry `com.codegen.run.id` from creation, so they are found and removed
    without reconstructing the context the harness had — the fragile round-trip
    below (`issue:6b4cae67568ff1d8bf82`) no longer decides what Docker keeps.

    It runs *behind* `fence_run_work`, never in front of it. A sweep that
    overtakes the fence is not a faster cleanup, it is a cleanup racing the run
    it is cleaning: the labelled worker it captures and force-removes may still
    be working for a run nothing has cancelled.

    Evidence first, as everywhere else: one capture pass over what is left of
    the run is taken and retained before anything is removed, and it is that
    pass which decides whether a `worker:meta` key retained for attribution may
    finally be deleted. A worker the run's label still lists and the pass could
    not read is written down as a stated missed capture rather than removed
    unnamed, and the artifact is merged into rather than replaced — the manifest
    round-trip below makes a second, poorer pass over the same run once these
    resources are gone, and it may not unsay what this one recorded.
    """
    _live_helpers_path()
    from run_cleanup import (
        RunCleanupError,
        account_listed_workers,
        accounted_workers,
        clean_run,
        docker_cli_ops,
        retain_evidence,
    )
    from run_evidence import RunEvidenceCollector

    root = Path(ORCHESTRATOR_ROOT)
    ops = docker_cli_ops(root)
    collector = RunEvidenceCollector(run_id=run_id, owned_workers=lambda: ops.meta_workers(run_id))
    try:
        collector.capture()
        account_listed_workers(collector, ops, run_id)
        retain_evidence(collector, root / ".live-manifests" / "evidence" / f"{run_id}.json")
    except Exception as exc:
        return [f"run {run_id} evidence capture: {exc}"]
    try:
        clean_run(ops, run_id, accounted_workers=accounted_workers(collector))
    except RunCleanupError as exc:
        return [str(exc)]
    except Exception as exc:
        return [f"run {run_id} cleanup: {type(exc).__name__}: {exc}"]
    return []


def recover_ownership_manifests() -> None:
    """Delete manifests only after owned resources are proven absent.

    Four phases, in this order, for each manifest: fence the run's work, capture
    what is left of it, remove only what is accounted for, verify the run left
    nothing. Each is a precondition of the next, so a manifest whose fence could
    not be established is reported and otherwise left alone — nothing of that
    run is captured or removed while it may still be live.
    """
    failures: list[str] = []
    manifest_dir = Path(ORCHESTRATOR_ROOT) / ".live-manifests"
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            resources = data.get("resources", [])
            if not resources:
                path.unlink()
                continue
            ctx = manifest_context(data)
            errors = fence_run_work(ctx)
            if errors:
                failures.extend(f"{path.name}: {error}" for error in errors)
                continue
            errors = cleanup_run_scoped_resources(str(data["run_id"]))
            errors += cleanup_manifest_resources(ctx)
            if errors:
                failures.extend(f"{path.name}: {error}" for error in errors)
            elif path.exists():
                path.unlink()
        except Exception as exc:
            failures.append(f"{path.name}: {type(exc).__name__}: {exc}")
    if failures:
        raise CleanupFailure("unproven manifest resources: " + "; ".join(failures))


def get_test_projects():
    conditions = _build_conditions()
    sql = f"SELECT id, title, slug FROM projects WHERE {conditions};"  # noqa: S608
    res = run_cmd(
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
            "-t",
            "-A",
            "-c",
            sql,
        ]
    )
    if res.returncode != 0:
        print(f"Failed to fetch projects: {res.stderr}")
        return []

    projects = []
    # psql separates rows with real newlines: splitting on the two-character
    # string `\n` collapsed every multi-project answer into one unparsable line
    # and silently returned no projects at all.
    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("|")
        expected_columns = 3
        if len(parts) == expected_columns:
            projects.append({"id": parts[0], "title": parts[1], "slug": parts[2]})
    return projects


def contour_repo_residue(names: list[str]) -> list[str]:
    """Repository names in the organization this contour owns.

    Repositories are named by project slug, so this matches the same truncated
    prefixes a deployed stack is matched by, and the same 32-hex project id — a
    repository merely starting like a test project is not residue.
    """
    return sorted(name for name in names if _STACK_NAME_PATTERN.match(name))


def list_org_repositories() -> list[str]:
    """Ask GitHub what is actually there, whatever the database knows.

    The database-driven half only sees repositories whose project row still
    exists. A run killed between deleting its rows and deleting its repository —
    or one whose teardown failed halfway — leaves a repository nothing can name
    afterwards, and production's own sync then alerts about it forever.
    """
    script = f"""
import asyncio, sys
sys.path.insert(0, '/app')
from shared.clients.github import GitHubAppClient
import httpx

async def listing():
    gh = GitHubAppClient()
    token = await gh.get_org_token('{GITHUB_ORG}')
    names = []
    async with httpx.AsyncClient() as client:
        page = 1
        while True:
            resp = await client.get(
                f"https://api.github.com/orgs/{GITHUB_ORG}/repos?per_page=100&page={{page}}",
                headers={{'Authorization': f'token {{token}}'}},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            names.extend(repo['name'] for repo in batch)
            page += 1
    for name in names:
        print(name)

asyncio.run(listing())
"""
    result = run_cmd(
        ["docker", "compose", "exec", "-T", "api", "python", "-c", script],
        check=False,
    )
    if result.returncode != 0:
        raise CleanupFailure(f"could not list the organization's repositories: {result.stderr}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def delete_github_repos(repo_names):
    """Delete this contour's repositories: the known ones and the stranded ones.

    `repo_names` comes from the database and therefore names only repositories
    whose project row still exists. The organization is asked as well, because a
    run killed between deleting its rows and deleting its repository leaves one
    that nothing can name afterwards — and, while contours share an
    organization, production's sync then alerts about that orphan forever.
    """
    residue = contour_repo_residue(list_org_repositories())
    orphans = sorted(set(residue) - set(repo_names))
    if orphans:
        print(f"{len(orphans)} repositories have no project row: {', '.join(orphans)}")
    repo_names = sorted(set(repo_names) | set(residue))

    if not repo_names:
        return
    print(f"Deleting {len(repo_names)} GitHub repositories...")
    repos_json = json.dumps(repo_names)
    script = f"""
import asyncio, sys
sys.path.insert(0, '/app')
from shared.clients.github import GitHubAppClient
import httpx
import json

async def cleanup():
    gh = GitHubAppClient()
    try:
        token = await gh.get_org_token('{GITHUB_ORG}')
    except Exception as e:
        print(f"Failed to get GitHub token: {{e}}")
        return

    repos = {repos_json}
    async with httpx.AsyncClient() as client:
        for repo_name in repos:
            print(f"API: Deleting '{{repo_name}}'...")
            resp = await client.delete(
                f"https://api.github.com/repos/{GITHUB_ORG}/{{repo_name}}",
                headers={{
                    'Authorization': f'token {{token}}',
                    'Accept': 'application/vnd.github+json',
                }},
            )
            if resp.status_code not in (204, 404):
                print(f"Failed to delete {{repo_name}}: {{resp.status_code}} {{resp.text[:200]}}")
            else:
                print(f"Deleted {{repo_name}}.")

asyncio.run(cleanup())
"""
    res = run_cmd(["docker", "compose", "exec", "-T", "langgraph", "python", "-c", script])
    print(res.stdout)
    if res.stderr:
        print(res.stderr)


def clean_database():
    conditions = _build_conditions()
    sub = f"SELECT id FROM projects WHERE {conditions}"  # noqa: S608
    tables = [
        "runs",
        "product_briefs",
        "tasks",
        "stories",
        "brainstorms",
        "rag_chunks",
        "rag_documents",
        "rag_conversation_summaries",
        "rag_messages",
        "service_deployments",
    ]
    stmts = [
        f"DELETE FROM task_events WHERE task_id IN ("  # noqa: S608
        f"SELECT t.id FROM tasks t JOIN projects p ON t.project_id = p.id "
        f"WHERE {_build_conditions('p')});",
        "DELETE FROM requirement_coverages WHERE brief_id IN ("
        "SELECT b.id FROM product_briefs b "
        "JOIN projects p ON b.project_id = p.id "
        f"WHERE {_build_conditions('p')});",
    ]
    stmts.extend(f"DELETE FROM {t} WHERE project_id IN ({sub});" for t in tables)  # noqa: S608
    stmts.append(
        "DELETE FROM port_allocations WHERE application_id IN "
        f"(SELECT a.id FROM applications a JOIN repositories r ON r.id = a.repo_id "
        f"JOIN projects p ON p.id = r.project_id WHERE {_build_conditions('p')});"
    )
    # application_health_history FKs applications (NO ACTION), delete it first.
    stmts.append(
        "DELETE FROM application_health_history WHERE application_id IN "
        f"(SELECT a.id FROM applications a JOIN repositories r ON r.id = a.repo_id "
        f"JOIN projects p ON p.id = r.project_id WHERE {_build_conditions('p')});"
    )
    stmts.append(
        f"DELETE FROM applications WHERE repo_id IN "  # noqa: S608
        f"(SELECT id FROM repositories WHERE project_id IN ({sub}));"
    )
    stmts.append(f"DELETE FROM repositories WHERE project_id IN ({sub});")
    stmts.append(f"DELETE FROM projects WHERE {conditions};")  # noqa: S608
    # The synthetic test user is a fixture reused by every run, not residue of
    # one, and the attempt ledger that references it is append-only by design —
    # a database rule refuses to delete from it, and rightly so. So the user goes
    # only while nothing points at it; once a run has recorded an attempt, the
    # row stays and the next run reuses it.
    #
    # Deleting the user unconditionally made the whole sweep raise, and a raising
    # sweep is not a partial one: every phase after the database went unrun and
    # its residue stayed on the stand.
    stmts.append(
        "DELETE FROM users WHERE telegram_id = 999000001 "
        "AND NOT EXISTS (SELECT 1 FROM engineering_attempt_ledger l WHERE l.user_id = users.id);"
    )
    sql = "\n".join(stmts)
    result = run_cmd(
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
        ]
    )
    if result.returncode != 0:
        raise CleanupFailure(f"database cleanup failed: {result.stderr.strip()}")
    print("Database cleaned.")


def clean_redis_queues(project_ids):
    """Delete only proven live-test entries from the canonical capability streams."""
    live_path = str(Path(ORCHESTRATOR_ROOT) / "tests" / "live")
    if live_path not in sys.path:
        sys.path.insert(0, live_path)
    from capability_cleanup import cleanup_owned_capability_messages

    def command(*args):
        result = run_cmd(["docker", "compose", "exec", "-T", "redis", "redis-cli", *args])
        if result.returncode != 0:
            raise CleanupFailure(f"capability stream cleanup failed: {result.stderr.strip()}")
        return result.stdout.strip()

    for project_id in project_ids:
        cleanup_owned_capability_messages(project_id, set(), command=command)
    print("Owned capability stream entries removed and verified.")


def _internal_api_headers() -> dict[str, str]:
    try:
        internal_key = os.environ["INTERNAL_API_KEY"]
    except KeyError as exc:
        raise CleanupFailure("INTERNAL_API_KEY is required for remote server cleanup") from exc
    return {"X-Internal-Key": internal_key}


def _fetch_remote_servers() -> list[dict]:
    import httpx

    try:
        with httpx.Client(
            base_url=CLEANUP_API_URL, headers=_internal_api_headers(), timeout=10
        ) as client:
            resp = client.get("/api/servers/")
            if resp.status_code != HTTP_OK:
                raise CleanupFailure(
                    f"server list fetch failed: {resp.status_code} {resp.text[:200]}"
                )
            servers = resp.json()
            if not isinstance(servers, list):
                raise CleanupFailure("server list fetch returned a non-list response")
            targets = managed_cleanup_targets(servers)
            if not targets:
                raise CleanupFailure("server list fetch returned no managed cleanup target")
            return [validate_managed_cleanup_target(target) for target in targets]
    except CleanupFailure:
        raise
    except Exception as exc:
        raise CleanupFailure(f"server list fetch failed: {exc}") from exc


def _fetch_remote_server_key(handle: str) -> str:
    import httpx

    try:
        with httpx.Client(
            base_url=CLEANUP_API_URL, headers=_internal_api_headers(), timeout=10
        ) as client:
            resp = client.get(f"/api/servers/{handle}/ssh-key")
            if resp.status_code != HTTP_OK:
                raise CleanupFailure(
                    f"ssh key fetch failed for {handle}: {resp.status_code} {resp.text[:200]}"
                )
            key = resp.json().get("ssh_key")
            if not isinstance(key, str) or not key.strip():
                raise CleanupFailure(f"ssh key fetch failed for {handle}: empty ssh_key")
            return key
    except CleanupFailure:
        raise
    except Exception as exc:
        raise CleanupFailure(f"ssh key fetch failed for {handle}: {exc}") from exc


@contextmanager
def _server_key_file(handle: str):
    """Materialise one server's SSH key for the duration of a block."""
    key = _fetch_remote_server_key(handle)
    if not key.endswith("\n"):
        key += "\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(key)
        key_path = f.name
    os.chmod(key_path, 0o600)
    try:
        yield key_path
    finally:
        os.unlink(key_path)


def _ssh(server: dict, key_path: str, command: str, stdin: str | None = None):
    return subprocess.run(
        [  # noqa: S607
            "ssh",
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "BatchMode=yes",
            f"{server['ssh_user']}@{server['public_ip']}",
            command,
        ],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def parse_remote_residue(output: str) -> list[str]:
    """Return the live-test stack artefacts one target reported."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def stack_names_from_residue(findings: list[str]) -> set[str]:
    """Recover cleanable stack names from reported containers and directories.

    A service directory *is* the stack name, so it is taken whole — everything
    the inventory reports must be something the sweep can then remove, or
    `make test-live-clean` would fail forever on residue it refuses to name.
    A container is named `<stack>-<service>-<n>` (Docker may render the stack's
    dashes as underscores), so its stack is read off the project UUID a slug
    always ends in rather than guessed at a separator.
    """
    names: set[str] = set()
    for finding in findings:
        kind, _, artefact = finding.partition(" ")
        artefact = artefact.rstrip("/").rsplit("/", 1)[-1]
        if kind == "directory":
            names.add(artefact)
            continue
        match = _STACK_NAME_PATTERN.match(artefact)
        if match:
            names.add(match.group(0).replace("_", "-"))
    return names


def collect_remote_residue() -> dict[str, list[str]]:
    """Inventory live-test stacks on every target, independent of the database.

    The DB-driven sweep can only clean slugs it still has rows for, so a run that
    died between deploying a stack and recording it — or after its rows were
    already deleted — leaves an orphan nothing else looks for. This asks each
    target directly.
    """
    residue: dict[str, list[str]] = {}
    command = build_remote_residue_command(DEPLOY_SLUG_PREFIXES)
    for server in _fetch_remote_servers():
        with _server_key_file(server["handle"]) as key_path:
            result = _ssh(server, key_path, command)
        if result.returncode != 0:
            raise CleanupFailure(
                f"remote residue scan failed for {server['handle']}: "
                f"{result.returncode} {result.stderr.strip()[:300]}"
            )
        findings = parse_remote_residue(result.stdout)
        if findings:
            residue[server["handle"]] = findings
    return residue


def clean_remote_servers(project_slugs: list[str] | None = None):
    servers = _fetch_remote_servers()

    if not servers:
        print("No remote servers found to clean.")
        return

    if project_slugs is None:
        project_slugs = [p["slug"] for p in get_test_projects()]

    remote_script = REMOTE_CLEANUP_SCRIPT.read_text()
    residue_command = build_remote_residue_command(DEPLOY_SLUG_PREFIXES)

    for s in servers:
        print(f"Cleaning remote server {s['handle']} ({s['ssh_user']}@{s['public_ip']})...")
        try:
            with _server_key_file(s["handle"]) as key_path:
                # The DB knows only the slugs whose rows survived. Ask the target
                # what it is actually running: a global sweep may name stacks by
                # prefix, and an orphan has no other name left.
                scan = _ssh(s, key_path, residue_command)
                if scan.returncode != 0:
                    raise CleanupFailure(
                        f"remote residue scan failed for {s['handle']}: "
                        f"{scan.returncode} {scan.stderr.strip()[:300]}"
                    )
                orphans = stack_names_from_residue(parse_remote_residue(scan.stdout))
                if orphans:
                    print(f"Found unlisted live-test stacks: {', '.join(sorted(orphans))}")
                targets = sorted(set(project_slugs) | orphans)
                if not targets:
                    print("No live-test stacks found on this server.")
                    continue
                for project_slug in targets:
                    r = _ssh(
                        s, key_path, build_remote_cleanup_command(project_slug), stdin=remote_script
                    )
                    print(r.stdout.strip())
                    if r.returncode != 0:
                        raise CleanupFailure(
                            f"remote cleanup failed for {s['handle']}/{project_slug}: "
                            f"{r.returncode} {r.stderr.strip()[:300]}"
                        )
                prune = _ssh(s, key_path, "docker network prune -f 2>&1 || true")
                print(prune.stdout.strip())
        except CleanupFailure:
            raise
        except Exception as e:
            raise CleanupFailure(f"failed to clean remote server {s['handle']}: {e}") from e


def clean_local_docker():
    # `docker ps --filter name=` takes a regexp, so the alternation is a bare
    # `|`; the escaped form matched a literal pipe and therefore nothing.
    patterns = "|".join(PROJECT_PREFIXES)
    res = run_cmd(["docker", "ps", "-aq", "--filter", f"name={patterns}"])
    containers = res.stdout.strip().split()
    if containers:
        print(f"Removing {len(containers)} local test worker containers...")
        run_cmd(["docker", "rm", "-f"] + containers)
    else:
        print("No local test containers found.")

    print("Pruning local networks...")
    run_cmd(["docker", "network", "prune", "-f"])


def clean_local_workspaces():
    sql = "SELECT id FROM repositories;"
    res = run_cmd(
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
            "-t",
            "-A",
            "-c",
            sql,
        ]
    )
    # Real newlines again: one collapsed line matched no repository id, so every
    # live workspace looked orphaned and was removed.
    active_repos = {line.strip() for line in res.stdout.strip().splitlines() if line.strip()}

    script = f"""
import os
import shutil

ACTIVE_REPOS = {list(active_repos)}

def scan_and_clean():
    count = 0
    path = "/data/workspaces"
    if os.path.exists(path):
        for d in os.listdir(path):
            if d.startswith("repo-") and d not in ACTIVE_REPOS:
                full_path = os.path.join(path, d)
                print(f"Removing orphaned workspace: {{full_path}}")
                shutil.rmtree(full_path, ignore_errors=True)
                count += 1
                
    tmp_path = "/tmp/codegen/workspaces"
    if os.path.exists(tmp_path):
        for d in os.listdir(tmp_path):
            if "worker" in d or "test" in d:
                full_path = os.path.join(tmp_path, d)
                shutil.rmtree(full_path, ignore_errors=True)
    
    print(f"Orphaned workspaces removed: {{count}}")

scan_and_clean()
"""
    # Run in root privileged container to bypass permissions issues (some files might be root-owned)
    res = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/data/workspaces:/data/workspaces",
            "-v",
            "/tmp/codegen/workspaces:/tmp/codegen/workspaces",  # noqa: S108
            "python:3-alpine",
            "python3",
            "-c",
            script,
        ]
    )
    print(res.stdout.strip())
    if res.stderr:
        print(res.stderr.strip())


def main():
    manifest_projects = manifest_project_ids()

    print_step("Recovering ownership manifests")
    # Fail-closed, but not fail-first. An unprovable manifest still makes this
    # run red (below, before any success is printed), yet it must not stop the
    # sweeps that follow: a manifest naming a deploy cannot be proven clean when
    # no server is registered, and that used to abort cleanup at its first step
    # with hand-deleting the manifest as the only way out. Everything the DB,
    # GitHub, Redis and target sweeps can still remove is now removed anyway.
    try:
        recover_ownership_manifests()
        manifest_recovery_failure = None
    except CleanupFailure as exc:
        manifest_recovery_failure = exc
        print(f"Ownership manifest recovery failed, continuing with the sweeps: {exc}")

    print_step("Identifying test projects")
    projects = get_test_projects()
    print(f"Found {len(projects)} test projects.")

    repo_names = [p["slug"] for p in projects]
    project_slugs = [p["slug"] for p in projects]
    project_ids = sorted(manifest_projects | {p["id"] for p in projects})

    print_step("Fencing and cleaning owned Redis capability work")
    clean_redis_queues(project_ids)

    print_step("Cleaning GitHub repositories")
    delete_github_repos(repo_names)

    print_step("Cleaning Remote Servers")
    clean_remote_servers(project_slugs)

    print_step("Cleaning database")
    clean_database()

    print_step("Cleaning Local Docker Test Containers")
    clean_local_docker()

    print_step("Cleaning Local Workspaces")
    clean_local_workspaces()

    if manifest_recovery_failure is not None:
        raise manifest_recovery_failure

    print_step("Verifying absence of live-test residue")
    verify_no_residue(project_ids)

    print("\n\033[1;32m✅ Live test cleanup fully complete!\033[0m")


if __name__ == "__main__":
    main()
