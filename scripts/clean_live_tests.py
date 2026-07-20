# ruff: noqa: S608
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

PROJECT_PREFIXES = ["live-test", "live-crud", "mega-test"]
CLEANUP_API_URL = "http://localhost:8000"
HTTP_OK = 200
ORCHESTRATOR_ROOT = os.environ.get("ORCHESTRATOR_ROOT")
if not ORCHESTRATOR_ROOT:
    ORCHESTRATOR_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITHUB_ORG = "project-factory-organization"


class CleanupFailure(RuntimeError):
    """Live cleanup could not prove that all test-owned resources are absent."""


def print_step(msg):
    print(f"\\n\\033[1;34m=== {msg} ===\\033[0m")


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


def collect_residue_state(project_ids: list[str] | None = None):
    """Return live-test residue counts using current schema relationships."""
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
    residue = collect_residue_state(project_ids)
    remaining = {kind: count for kind, count in residue.items() if count}
    if remaining:
        details = ", ".join(f"{kind}={count}" for kind, count in sorted(remaining.items()))
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


def cleanup_manifest_resources(data: dict) -> list[str]:
    """Resume the same fail-closed owned cleanup used by the live harness."""
    import asyncio

    import httpx

    live_path = str(Path(ORCHESTRATOR_ROOT) / "tests" / "live")
    if live_path not in sys.path:
        sys.path.insert(0, live_path)
    from live_harness import OwnedResource, OwnershipManifest
    from pipeline_helpers import cleanup_all

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

    async def resume() -> None:
        # cleanup_all cancels runs via /api/runs/, which rejects unauthenticated
        # callers; authenticate as an internal service like the live harness does
        headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
        async with httpx.AsyncClient(
            base_url="http://localhost:8000", timeout=20, headers=headers
        ) as api:
            await cleanup_all(api, api, ctx)

    try:
        asyncio.run(resume())
    except Exception as exc:
        return [str(exc)]
    return []


def recover_ownership_manifests() -> None:
    """Delete manifests only after owned resources are proven absent."""
    failures: list[str] = []
    manifest_dir = Path(ORCHESTRATOR_ROOT) / ".live-manifests"
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            resources = data.get("resources", [])
            if not resources:
                path.unlink()
                continue
            errors = cleanup_manifest_resources(data)
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
    for line in res.stdout.strip().split("\\n"):
        if not line:
            continue
        parts = line.split("|")
        expected_columns = 3
        if len(parts) == expected_columns:
            projects.append({"id": parts[0], "title": parts[1], "slug": parts[2]})
    return projects


def delete_github_repos(repo_names):
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
    stmts.append("DELETE FROM users WHERE telegram_id = 999000001;")
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
            return servers
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
            if not isinstance(key, str) or not key:
                raise CleanupFailure(f"ssh key fetch failed for {handle}: empty ssh_key")
            return key
    except CleanupFailure:
        raise
    except Exception as exc:
        raise CleanupFailure(f"ssh key fetch failed for {handle}: {exc}") from exc


def _build_remote_sweep_command(project_slugs: list[str]) -> str:
    quoted_projects = shlex.quote("\n".join(sorted(set(project_slugs))))
    return f"""
set -eu
PROJECTS={quoted_projects}
echo '[Remote] Cleaning live-test project resources...'
for project_name in $PROJECTS; do
  alt_project_name=$(printf '%s' "$project_name" | tr '-' '_')
  projects=$(printf '%s\\n%s\\n' "$project_name" "$alt_project_name" | awk 'NF && !seen[$0]++')
  svc_dir="/opt/services/$project_name"
  for project in $projects; do
    for c in $(docker ps -aq --filter "name=^/${{project}}[-_]"); do
      label=$(docker inspect -f '{{{{ index .Config.Labels "com.docker.compose.project" }}}}' "$c")
      if [ -n "$label" ] && [ "$label" != "<no value>" ]; then
        projects=$(printf '%s\\n%s\\n' "$projects" "$label" | awk 'NF && !seen[$0]++')
      fi
    done
  done
  if [ -d "$svc_dir/infra" ]; then
    for project in $projects; do
      (cd "$svc_dir/infra" && docker compose -p "$project" down --remove-orphans -v)
    done
  fi
  for project in $projects; do
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
  rm -rf "$svc_dir"
  remaining=
  for project in $projects; do
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
  test ! -e "$svc_dir"
done
docker network prune -f 2>&1 || true
""".strip()


def clean_remote_servers(project_slugs: list[str] | None = None):
    servers = _fetch_remote_servers()

    if not servers:
        print("No remote servers found to clean.")
        return

    if project_slugs is None:
        project_slugs = [p["slug"] for p in get_test_projects()]
    if not project_slugs:
        print("No live-test project slugs found for remote cleanup.")
        return

    remote_cmd = _build_remote_sweep_command(project_slugs)

    for s in servers:
        ip = s["public_ip"]
        ssh_user = s["ssh_user"]
        key = _fetch_remote_server_key(s["handle"])
        if not key.endswith("\\n"):
            key += "\\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(key)
            key_path = f.name
        os.chmod(key_path, 0o600)

        print(f"Cleaning remote server {s['handle']} ({ssh_user}@{ip})...")
        try:
            r = subprocess.run(
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
                    f"{ssh_user}@{ip}",
                    remote_cmd,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            print(r.stdout.strip())
            if r.returncode != 0:
                raise CleanupFailure(
                    f"remote cleanup failed for {s['handle']}: "
                    f"{r.returncode} {r.stderr.strip()[:300]}"
                )
        except CleanupFailure:
            raise
        except Exception as e:
            raise CleanupFailure(f"failed to clean remote server {s['handle']}: {e}") from e
        finally:
            os.unlink(key_path)


def clean_local_docker():
    patterns = "\\|".join(PROJECT_PREFIXES)
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
    active_repos = {line.strip() for line in res.stdout.strip().split("\\n") if line.strip()}

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
    recover_ownership_manifests()

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

    print_step("Verifying absence of live-test residue")
    verify_no_residue(project_ids)

    print("\\n\\033[1;32m✅ Live test cleanup fully complete!\\033[0m")


if __name__ == "__main__":
    main()
