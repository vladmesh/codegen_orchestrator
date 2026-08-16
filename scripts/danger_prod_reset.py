#!/usr/bin/env python3
"""Destructive full reset of the production contours.

Until there is a separate stand, a live end-to-end run happens on production, and
the run leaves traces on seven surfaces: the deploy target, the project's GitHub
repository, the local registry, fifteen database tables, Redis keys, unread queue
messages and a workspace on disk. Removing them by hand takes an operator half an
hour of careful work, and having an agent do it costs tokens for something that
needs no judgement at all.

Deleting everything and building it back from the revision that is already
deployed is both faster and more honest: the end state is defined by the code, not
by how thorough the person doing the cleanup was.

Order of operations, and why:

  1. inventory  — read what exists before touching anything, so the operator
                  confirms against real numbers rather than an intention
  2. guards     — refuse unless the phrase was typed and no unknown user owns data
  3. github     — delete organization repositories outside the keep-list
  4. targets    — stop every project stack on every managed target and wipe it
  5. control    — drop the database, Redis, registry and workspaces, then bring the
                  stack back, migrate and re-seed from the same revision
  6. verify     — prove the contours are empty instead of assuming it

What this deliberately does NOT do is reinstall the target's operating system. The
docker-level wipe reaches the same end state in about a minute, while a reinstall
takes ten to fifteen and hands control to the provisioner, which picks the
reinstall path by its own rules (services/infra-service/src/provisioner/node.py).
Re-provisioning is a test of provisioning, not a cleanup, and it deserves to be
run on purpose rather than as a side effect of tidying up.

Usage:

    python3 scripts/danger_prod_reset.py --dry-run
    python3 scripts/danger_prod_reset.py --confirm DANGEROUS-PROD-CLEANUP-MEGA-SUPER-PUPER
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys

CONFIRMATION_PHRASE = "DANGEROUS-PROD-CLEANUP-MEGA-SUPER-PUPER"

DEFAULT_ORCHESTRATOR_HOST = "5wce.l.time4vps.cloud"
DEFAULT_SSH_USER = "root"
DEPLOY_PATH = "/opt/codegen_orchestrator"
WORKSPACE_ROOT = "/data/workspaces"
GITHUB_ORG = "project-factory-organization"

# Repositories the reset must never delete. fortune-teller-bot predates the
# factory and belongs to the owner, not to a run.
DEFAULT_KEEP_REPOS = ("fortune-teller-bot",)

# caddy-data holds issued TLS certificates and grafana/loki hold monitoring
# history: neither is residue of a run, and re-issuing certificates hits Let's
# Encrypt rate limits.
DROPPED_VOLUMES = (
    "codegen_orchestrator_db_data",
    "codegen_orchestrator_redis_data",
    "codegen_orchestrator_registry-data",
)

COMPOSE = (
    f"docker compose -f {DEPLOY_PATH}/docker-compose.yml -f {DEPLOY_PATH}/docker-compose.prod.yml"
)
API_CONTAINER = "codegen_orchestrator-api-1"
DB_CONTAINER = "codegen_orchestrator-db-1"

# Consumers first: a running consumer re-dispatches work while the database is
# being emptied under it.
CONSUMER_CONTAINERS = (
    "codegen_orchestrator-scheduler-1",
    "codegen_orchestrator-engineering-worker-1",
    "codegen_orchestrator-deploy-worker-1",
    "codegen_orchestrator-qa-worker-1",
    "codegen_orchestrator-architect-1",
    "codegen_orchestrator-scaffolder-1",
)


class ResetFailure(RuntimeError):
    """A step failed in a way that makes continuing unsafe."""


# --------------------------------------------------------------------------
# Decisions. Kept free of side effects so they can be tested without a server.
# --------------------------------------------------------------------------


def parse_server_ids(raw: str | None) -> set[int]:
    """Provider ids from TIME4VPS_MANAGED_SERVER_IDS, ignoring blanks."""
    if not raw:
        return set()
    ids = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            ids.add(int(chunk))
    return ids


def managed_handles(server_ids: set[int]) -> set[str]:
    """Server handles the reset is allowed to wipe."""
    return {f"vps-{server_id}" for server_id in sorted(server_ids)}


def wipeable_servers(servers: list[dict], allowed_handles: set[str]) -> list[dict]:
    """Managed servers that are also present in the allowlist.

    A server missing from TIME4VPS_MANAGED_SERVER_IDS is somebody else's machine
    as far as this script is concerned, even when the database calls it managed.
    """
    return [
        server
        for server in servers
        if server.get("is_managed") and server.get("handle") in allowed_handles
    ]


def repos_to_delete(repo_names: list[str], keep: tuple[str, ...]) -> list[str]:
    """Organization repositories that the reset deletes."""
    return [name for name in repo_names if name not in set(keep)]


def unexpected_owners(users: list[dict], allowed_telegram_ids: set[int]) -> list[dict]:
    """Users the operator did not declare as their own test accounts.

    The reset destroys every project in the database. Once real users exist, a
    single unknown account is enough reason to stop and make the operator say
    out loud whose data is being deleted.
    """
    return [user for user in users if int(user.get("telegram_id", 0)) not in allowed_telegram_ids]


def confirmation_matches(typed: str | None) -> bool:
    return typed == CONFIRMATION_PHRASE


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


class Remote:
    """Commands on the orchestrator host."""

    def __init__(self, host: str, user: str, ssh_key: str | None, dry_run: bool) -> None:
        self.host = host
        self.user = user
        self.ssh_key = ssh_key
        self.dry_run = dry_run

    def _ssh_argv(self, command: str) -> list[str]:
        argv = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15"]
        if self.ssh_key:
            argv += ["-i", self.ssh_key]
        argv.append(f"{self.user}@{self.host}")
        argv.append(command)
        return argv

    def run(self, command: str, *, check: bool = True, mutating: bool = True) -> str:
        """Run a command on the host. Reads still run under --dry-run."""
        if self.dry_run and mutating:
            print(f"    [dry-run] {command}")
            return ""
        result = subprocess.run(
            self._ssh_argv(command),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise ResetFailure(
                f"command failed ({result.returncode}): {command}\n{result.stderr.strip()}"
            )
        return result.stdout

    def api(self, method: str, path: str, payload: dict | None = None) -> str:
        """Call the internal API from inside the api container.

        The key is read from .env on the host and handed to the container as an
        environment variable, so it never appears in an argument list. The curl
        runs under `sh -c` *inside* the container: without it the host shell
        expands $K first and the header goes out empty, which the API answers
        with «Authentication required».
        """
        inner = (
            f'curl -sS -X {method} -H "X-Internal-Key: $K" '
            '-H "Content-Type: application/json" '
            + (f"-d {shlex.quote(json.dumps(payload))} " if payload is not None else "")
            + f"http://localhost:8000{path}"
        )
        command = (
            f"set -a; . {DEPLOY_PATH}/.env; set +a; "
            f'docker exec -e K="$INTERNAL_API_KEY" {API_CONTAINER} sh -c {shlex.quote(inner)}'
        )
        return self.run(command, mutating=method != "GET")

    def psql(self, sql: str) -> str:
        psql = f"docker exec {DB_CONTAINER} psql -U postgres -d orchestrator -tA"
        return self.run(f"{psql} -c {shlex.quote(sql)}", mutating=False)


def step(title: str) -> None:
    print(f"\n=== {title}")


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def collect_inventory(remote: Remote) -> dict:
    step("Inventory")
    servers = json.loads(remote.api("GET", "/api/servers/") or "[]")
    users = json.loads(remote.api("GET", "/api/users/") or "[]")
    counts = remote.psql(
        "select (select count(*) from projects), (select count(*) from applications), "
        "(select count(*) from runs), (select count(*) from stories)"
    ).strip()
    projects, applications, runs, stories = (counts or "0|0|0|0").split("|")
    repos = json.loads(
        remote.run(
            f"docker exec {API_CONTAINER} python -c "
            + shlex.quote(
                "import asyncio, json\n"
                "import shared.clients.github as g\n"
                "async def main():\n"
                "    c = g.GitHubAppClient()\n"
                f"    repos = await c.list_org_repos({GITHUB_ORG!r})\n"
                "    print(json.dumps([r.name for r in repos]))\n"
                "asyncio.run(main())\n"
            ),
            mutating=False,
        ).strip()
        or "[]"
    )
    disk = remote.run("df -h / | tail -1", mutating=False).strip()

    inventory = {
        "servers": servers,
        "users": users,
        "projects": int(projects),
        "applications": int(applications),
        "runs": int(runs),
        "stories": int(stories),
        "repos": repos,
        "disk": disk,
    }
    print(
        f"  projects={inventory['projects']} applications={inventory['applications']} "
        f"runs={inventory['runs']} stories={inventory['stories']}"
    )
    print(f"  users: {[u.get('telegram_id') for u in users]}")
    print(f"  org repositories: {repos}")
    print(f"  servers: {[s.get('handle') for s in servers]}")
    print(f"  orchestrator disk: {disk}")
    return inventory


def enforce_guards(inventory: dict, args: argparse.Namespace) -> None:
    step("Guards")
    if not confirmation_matches(args.confirm):
        raise ResetFailure(
            f"refusing to touch production: pass --confirm {CONFIRMATION_PHRASE} to say it out loud"
        )
    allowed = set(args.allow_telegram_id or [])
    strangers = unexpected_owners(inventory["users"], allowed)
    if strangers and not args.force_unknown_users:
        listed = ", ".join(str(u.get("telegram_id")) for u in strangers)
        raise ResetFailure(
            f"refusing to delete data owned by undeclared users: {listed}. "
            "Pass --allow-telegram-id for each account you own, or "
            "--force-unknown-users if you mean to delete a real user's projects."
        )
    print("  confirmation accepted")
    print(f"  declared accounts: {sorted(allowed) or 'none'}")
    if strangers:
        print(
            f"  WARNING: deleting data of undeclared users: "
            f"{[u.get('telegram_id') for u in strangers]}"
        )


def purge_github(remote: Remote, inventory: dict, keep: tuple[str, ...]) -> None:
    step("GitHub organization")
    doomed = repos_to_delete(inventory["repos"], keep)
    if not doomed:
        print(f"  nothing to delete (kept: {list(keep)})")
        return
    print(f"  deleting {len(doomed)}: {doomed}")
    script = (
        "import asyncio, json, sys\n"
        "import shared.clients.github as g\n"
        f"doomed = {doomed!r}\n"
        "async def main():\n"
        "    c = g.GitHubAppClient()\n"
        "    for name in doomed:\n"
        f"        ok = await c.delete_repo({GITHUB_ORG!r}, name)\n"
        "        print(f'    {name}: {ok}')\n"
        "asyncio.run(main())\n"
    )
    print(remote.run(f"docker exec {API_CONTAINER} python -c {shlex.quote(script)}").rstrip())


def wipe_targets(remote: Remote, inventory: dict, allowed_handles: set[str]) -> None:
    step("Deploy targets")
    targets = wipeable_servers(inventory["servers"], allowed_handles)
    if not targets:
        print("  no managed target in the allowlist; nothing to wipe")
        return
    for server in targets:
        handle = server["handle"]
        ip = server.get("public_ip") or server.get("host")
        print(f"  {handle} ({ip})")
        key_path = f"/root/.reset-{handle}.pem"
        # The target's private key lives in the database, not on the host. It is
        # written out for the length of the wipe and shredded in the finally.
        inner = (
            f'curl -sS -H "X-Internal-Key: $K" '
            f"http://localhost:8000/api/servers/{handle}/ssh-key"
        )
        remote.run(
            f"set -a; . {DEPLOY_PATH}/.env; set +a; "
            f'docker exec -e K="$INTERNAL_API_KEY" {API_CONTAINER} sh -c {shlex.quote(inner)} '
            "| python3 -c 'import json,sys; print(json.load(sys.stdin)[\"ssh_key\"])' "
            f"> {key_path}; chmod 600 {key_path}"
        )
        remote_wipe = (
            "for d in /opt/services/*/; do "
            '[ -d "$d" ] && (cd "$d" && docker compose down -v --remove-orphans || true); '
            "done; "
            "rm -rf /opt/services/*; "
            "docker system prune -af --volumes > /dev/null; "
            'echo "  services: $(ls -A /opt/services | wc -l) entries"; '
            'echo "  containers: $(docker ps -aq | wc -l)"; '
            "df -h / | tail -1"
        )
        try:
            out = remote.run(
                f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i {key_path} "
                f"root@{ip} {shlex.quote(remote_wipe)}"
            )
            print("    " + out.strip().replace("\n", "\n    "))
        finally:
            remote.run(f"shred -u {key_path} 2>/dev/null || rm -f {key_path}", check=False)


def reset_control_plane(remote: Remote) -> None:
    step("Control plane")

    print("  stopping consumers")
    remote.run(f"docker stop {' '.join(CONSUMER_CONTAINERS)} > /dev/null", check=False)

    print("  saving server records before the database goes")
    dump = (
        "import json, subprocess\n"
        "servers = json.loads(subprocess.run(\n"
        f"    ['docker','exec','{API_CONTAINER}','curl','-sS','-H','X-Internal-Key: '+KEY,\n"
        "     'http://localhost:8000/api/servers/'], capture_output=True, text=True).stdout)\n"
        "for s in servers:\n"
        "    out = subprocess.run(\n"
        f"        ['docker','exec','{API_CONTAINER}','curl','-sS','-H','X-Internal-Key: '+KEY,\n"
        "         f\"http://localhost:8000/api/servers/{s['handle']}/ssh-key\"],\n"
        "        capture_output=True, text=True).stdout\n"
        "    try:\n"
        "        s['ssh_key'] = json.loads(out)['ssh_key']\n"
        "    except Exception:\n"
        "        s['ssh_key'] = None\n"
        f"open('{DEPLOY_PATH}/secrets/server_keys.json','w').write(json.dumps(servers))\n"
        "print(f'    saved {len(servers)} servers')\n"
    )
    remote.run(
        "set -a; . "
        + f"{DEPLOY_PATH}/.env; set +a; mkdir -p {DEPLOY_PATH}/secrets; "
        + "KEY=$INTERNAL_API_KEY python3 -c "
        + shlex.quote("import os\nKEY = os.environ['KEY']\n" + dump)
    )
    remote.run(f"chmod 600 {DEPLOY_PATH}/secrets/server_keys.json")

    print("  removing workers, stack and volumes")
    remote.run(
        'docker ps -a --filter "name=worker-" --format "{{.Names}}" '
        "| grep -v codegen_orchestrator | xargs -r docker rm -f > /dev/null",
        check=False,
    )
    remote.run(
        'docker images --filter "reference=worker:*" -q | xargs -r docker rmi -f > /dev/null',
        check=False,
    )
    remote.run(f"cd {DEPLOY_PATH} && {COMPOSE} down --remove-orphans")
    for volume in DROPPED_VOLUMES:
        remote.run(f"docker volume rm {volume}", check=False)
    remote.run(f"rm -rf {WORKSPACE_ROOT}/repo-*")

    print("  starting database and api, migrating")
    remote.run(f"cd {DEPLOY_PATH} && {COMPOSE} up -d db redis api")
    remote.run(
        f"cd {DEPLOY_PATH} && timeout 120 sh -c 'until {COMPOSE} exec -T api "
        "curl -sf http://localhost:8000/health > /dev/null; do sleep 2; done'"
    )
    remote.run(f"cd {DEPLOY_PATH} && {COMPOSE} exec -T api alembic upgrade head")

    # Both seed calls run shell-side so the values stay in the container
    # environment instead of an argument list.
    print("  seeding")
    remote.run(
        "set -a; . "
        + f"{DEPLOY_PATH}/.env; set +a; "
        + f'docker exec -e K="$INTERNAL_API_KEY" -e L="$TIME4VPS_LOGIN" '
        f'-e P="$TIME4VPS_PASSWORD" {API_CONTAINER} sh -c '
        + shlex.quote(
            'curl -sS -X POST -H "X-Internal-Key: $K" -H "Content-Type: application/json" '
            '-d "{\\"service\\": \\"time4vps\\", \\"type\\": \\"credentials\\", '
            '\\"value\\": {\\"username\\": \\"$L\\", \\"password\\": \\"$P\\"}}" '
            "http://localhost:8000/api/api-keys/ > /dev/null"
        ),
        check=False,
    )
    remote.run(
        "set -a; . "
        + f"{DEPLOY_PATH}/.env; set +a; "
        + f'docker exec -e K="$INTERNAL_API_KEY" -e A="$TELEGRAM_ID_ADMIN" {API_CONTAINER} sh -c '
        + shlex.quote(
            'curl -sS -X POST -H "X-Internal-Key: $K" -H "Content-Type: application/json" '
            '-d "{\\"telegram_id\\": $A, \\"username\\": \\"admin\\", '
            '\\"first_name\\": \\"Admin\\", \\"is_admin\\": true}" '
            "http://localhost:8000/api/users/ > /dev/null"
        ),
        check=False,
    )
    restore = (
        "import json, subprocess, os\n"
        "KEY = os.environ['KEY']\n"
        f"servers = json.load(open('{DEPLOY_PATH}/secrets/server_keys.json'))\n"
        "for s in servers:\n"
        "    payload = json.dumps({\n"
        "        'handle': s['handle'], 'host': s.get('host',''),\n"
        "        'public_ip': s.get('public_ip',''), 'ssh_user': s.get('ssh_user','root'),\n"
        "        'ssh_key': s.get('ssh_key'), 'is_managed': s.get('is_managed', True),\n"
        "        'status': s.get('status','active'), 'labels': s.get('labels', {}),\n"
        "    })\n"
        "    subprocess.run(\n"
        f"        ['docker','exec','{API_CONTAINER}','curl','-sS','-X','POST',\n"
        "         '-H','X-Internal-Key: '+KEY,'-H','Content-Type: application/json',\n"
        "         '-d',payload,'http://localhost:8000/api/servers/'],\n"
        "        capture_output=True, text=True)\n"
        "print(f'    restored {len(servers)} servers')\n"
    )
    remote.run(
        "set -a; . "
        + f"{DEPLOY_PATH}/.env; set +a; KEY=$INTERNAL_API_KEY python3 -c "
        + shlex.quote(restore)
    )
    for seeder, config in (
        ("seed_agent_configs.py", "agent_configs.yaml"),
        ("seed_system_configs.py", "system_configs.yaml"),
    ):
        remote.run(
            f"cd {DEPLOY_PATH} && {COMPOSE} exec -T api python /app/scripts/{seeder} "
            f"--api-base-url http://localhost:8000 --configs-path /app/scripts/{config}",
            check=False,
        )

    print("  starting the rest of the stack")
    remote.run(f"cd {DEPLOY_PATH} && {COMPOSE} up -d")


def verify(remote: Remote, keep: tuple[str, ...], allowed_handles: set[str]) -> list[str]:
    step("Verification")
    problems: list[str] = []

    repos = json.loads(
        remote.run(
            f"docker exec {API_CONTAINER} python -c "
            + shlex.quote(
                "import asyncio, json\n"
                "import shared.clients.github as g\n"
                "async def main():\n"
                "    c = g.GitHubAppClient()\n"
                f"    print(json.dumps([r.name for r in await c.list_org_repos({GITHUB_ORG!r})]))\n"
                "asyncio.run(main())\n"
            ),
            mutating=False,
        ).strip()
        or "[]"
    )
    print(f"  org repositories: {repos}")
    leftover_repos = repos_to_delete(repos, keep)
    if leftover_repos:
        problems.append(f"organization still holds {leftover_repos}")

    surviving = remote.psql(
        "select server_handle, count(*) from applications group by server_handle"
    ).strip()
    for line in filter(None, surviving.splitlines()):
        handle, count = line.split("|")
        if handle in allowed_handles:
            problems.append(f"{handle} still has {count} applications in the database")

    counts = remote.psql(
        "select (select count(*) from projects), (select count(*) from applications), "
        "(select count(*) from runs), (select count(*) from stories), "
        "(select count(*) from port_allocations)"
    ).strip()
    print(f"  db projects|applications|runs|stories|ports = {counts}")
    if counts and any(int(value) for value in counts.split("|")):
        problems.append(f"database is not empty: {counts}")

    workspaces = remote.run(
        f"ls -A {WORKSPACE_ROOT} 2>/dev/null | grep '^repo-' | wc -l", mutating=False
    ).strip()
    print(f"  workspaces left: {workspaces}")
    if workspaces != "0":
        problems.append(f"{workspaces} workspaces left behind")

    catalog = remote.run(
        "docker exec codegen_orchestrator-registry-1 "
        "wget -qO- http://localhost:5000/v2/_catalog 2>/dev/null || echo '{}'",
        mutating=False,
    ).strip()
    print(f"  registry: {catalog}")
    if '"repositories":[]' not in catalog.replace(" ", ""):
        problems.append(f"registry is not empty: {catalog}")

    unhealthy = remote.run(
        'docker ps --format "{{.Names}} {{.Status}}" | grep -c "Restarting" || true',
        mutating=False,
    ).strip()
    if unhealthy not in {"0", ""}:
        problems.append(f"{unhealthy} containers are restarting")

    print(f"  disk: {remote.run('df -h / | tail -1', mutating=False).strip()}")
    return problems


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Destructive reset of the production contours.",
        epilog=f"Confirmation phrase: {CONFIRMATION_PHRASE}",
    )
    parser.add_argument("--host", default=DEFAULT_ORCHESTRATOR_HOST)
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER)
    parser.add_argument("--ssh-key", default=None)
    parser.add_argument("--confirm", default=None, help="the phrase, typed out")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument(
        "--allow-telegram-id",
        type=int,
        action="append",
        help="a Telegram id whose data you own; repeatable",
    )
    parser.add_argument(
        "--force-unknown-users",
        action="store_true",
        help="delete data of accounts that were not declared",
    )
    parser.add_argument(
        "--keep-repo",
        action="append",
        default=None,
        help="organization repository to keep; repeatable",
    )
    parser.add_argument("--skip-github", action="store_true")
    parser.add_argument("--skip-targets", action="store_true")
    parser.add_argument("--skip-control-plane", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    keep = tuple(args.keep_repo) if args.keep_repo else DEFAULT_KEEP_REPOS
    remote = Remote(args.host, args.ssh_user, args.ssh_key, args.dry_run)

    print(f"Target orchestrator: {args.ssh_user}@{args.host}")
    if args.dry_run:
        print("DRY RUN: reads happen, nothing is destroyed")

    try:
        inventory = collect_inventory(remote)
        if not args.dry_run:
            enforce_guards(inventory, args)

        raw_ids = remote.run(
            f"grep -E '^TIME4VPS_MANAGED_SERVER_IDS=' {DEPLOY_PATH}/.env | cut -d= -f2-",
            check=False,
            mutating=False,
        ).strip()
        allowed_handles = managed_handles(parse_server_ids(raw_ids))
        print(f"  managed targets allowed: {sorted(allowed_handles) or 'none'}")

        if not args.skip_github:
            purge_github(remote, inventory, keep)
        if not args.skip_targets:
            wipe_targets(remote, inventory, allowed_handles)
        if not args.skip_control_plane:
            reset_control_plane(remote)

        if args.dry_run:
            print("\nDry run finished; nothing was changed.")
            return 0

        problems = verify(remote, keep, allowed_handles)
    except ResetFailure as failure:
        print(f"\nFAILED: {failure}", file=sys.stderr)
        return 1

    if problems:
        print("\nReset finished with residue:")
        for problem in problems:
            print(f"  - {problem}")
        return 2
    print("\nContours are clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
