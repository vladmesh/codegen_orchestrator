"""Production runs the image code: no container of a deploy contour mounts the checkout.

`docker-compose.yml` bind-mounts each service's source (`services/<svc>/src`, `shared`,
api's migrations and scripts, infra-service's ansible) over what its image baked, so a
developer's edit runs on restart. The deploy runs the released image digest instead
(`deployed-service-images.compose.yml`), and that digest is only what runs if nothing is
mounted over it. `docker-compose.prod.yml` therefore resets every source mount and restates
the runtime mounts; the stand stacks its own overlay on top of production's.

The stacks are rendered by `docker compose config`, fully merged, the same way
`test_secure_admin_entry.py` renders production — it needs the compose CLI, not a daemon.
Three things are pinned: no prod-contour container has a bind mount whose source is a
repository path other than third-party configuration (`infra/`) and the development key
fallback (`secrets/`); every runtime mount the base file declares survives on both
contours, the stand's one deliberate difference named; and the development stack keeps
its source mounts and local builds.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).parents[2]
BASE = ("docker-compose.yml",)
PROD = (*BASE, "docker-compose.prod.yml")
STAND = (*PROD, "docker-compose.stand.yml")

#: Repository paths a prod-contour container may bind-mount: configuration of the
#: third-party images (Caddy, Loki, Promtail, Grafana, the db init script) and the
#: development fallback of the GitHub App key path. Everything else in the repository is
#: our source, which production runs from the image.
NOT_SOURCE = ("infra", "secrets")

#: The runtime mounts production needs, by service and container path — named here so
#: that dropping one from the base file and the overlay together still fails.
GITHUB_APP_KEY = "/app/keys/github_app.pem"
REQUIRED_RUNTIME_MOUNTS = {
    "api": {GITHUB_APP_KEY},
    "langgraph": {GITHUB_APP_KEY},
    "engineering-worker": {GITHUB_APP_KEY},
    "deploy-worker": {GITHUB_APP_KEY},
    "scheduler-pipeline": {GITHUB_APP_KEY},
    "scheduler-maintenance": {GITHUB_APP_KEY},
    "scaffolder": {GITHUB_APP_KEY, "/data/workspaces", "/root/.cache/uv"},
    "worker-manager": {
        "/var/run/docker.sock",
        "/data/workspaces",
        "/data/worker-transcripts",
        "/host-claude",
        "/host-codex",
    },
    "db": {"/var/lib/postgresql/data"},
    "redis": {"/data"},
    "registry": {"/var/lib/registry"},
    "caddy": {"/data", "/config"},
    "loki": {"/loki"},
    "grafana": {"/var/lib/grafana"},
}

#: What the stand runs differently, on purpose (docker-compose.stand.yml): Claude keeps
#: its short-lived token, so the stand's worker-manager mounts no host Claude directory.
STAND_DROPS = {("worker-manager", "/host-claude")}

#: The source mounts the development stack keeps (the card's inventory).
DEV_SOURCE_MOUNTS = {
    "api": {
        ("services/api/src", "/app/src"),
        ("services/api/alembic.ini", "/app/alembic.ini"),
        ("services/api/migrations", "/app/migrations"),
        ("shared", "/app/shared"),
        ("scripts", "/app/scripts"),
    },
    **{
        name: {("services/langgraph/src", "/app/src"), ("shared", "/app/shared")}
        for name in ("langgraph", "architect", "engineering-worker", "deploy-worker", "qa-worker")
    },
    "infra-service": {
        ("services/infra-service/src", "/app/src"),
        ("services/infra-service/ansible", "/app/ansible"),
        ("shared", "/app/shared"),
    },
    "telegram_bot": {("services/telegram_bot/src", "/app/src"), ("shared", "/app/shared")},
    **{
        name: {("services/scheduler/src", "/app/src"), ("shared", "/app/shared")}
        for name in ("scheduler-pipeline", "scheduler-infrastructure", "scheduler-maintenance")
    },
    "scaffolder": {("services/scaffolder/src", "/app/src"), ("shared", "/app/shared")},
}


def _render(files: tuple[str, ...], project_dir: Path) -> dict:
    env_file = project_dir / ".env"
    shutil.copy(ROOT / ".env.example", env_file)
    # LOKI_URL as test_secure_admin_entry.py sets it; HOST_CODEX_HOME has no default in
    # the stand overlay, which fails fast on a stand .env without it.
    env_file.write_text(
        env_file.read_text()
        + "\nLOKI_URL=http://loki:3100\nHOST_CODEX_HOME=/opt/secrets/codex-stand\n"
    )
    command = ["docker", "compose", "--project-directory", str(project_dir)]
    command += ["--env-file", str(env_file)]
    for name in files:
        command += ["-f", str(ROOT / name)]
    result = subprocess.run(
        [*command, "config", "--format", "json"], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def stacks() -> dict[tuple[str, ...], tuple[dict, Path]]:
    """Each stack rendered once; relative sources resolve under a throwaway project dir."""
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp).resolve()
        yield {files: (_render(files, project_dir), project_dir) for files in (BASE, PROD, STAND)}


def _repository_path(source: str, project_dir: Path) -> PurePosixPath | None:
    """The repository-relative path a bind source names, or None when it is outside it."""
    for root in (project_dir, ROOT.resolve()):
        try:
            return PurePosixPath(Path(source).relative_to(root).as_posix())
        except ValueError:
            continue
    return None


def _source_mounts(config: dict, project_dir: Path) -> dict[str, set[tuple[str, str]]]:
    """Every bind mount of a repository path that is not third-party configuration."""
    found: dict[str, set[tuple[str, str]]] = {}
    for name, service in config["services"].items():
        for volume in service.get("volumes", []):
            if volume["type"] != "bind":
                continue
            path = _repository_path(volume["source"], project_dir)
            if path is None or (path.parts and path.parts[0] in NOT_SOURCE):
                continue
            found.setdefault(name, set()).add((str(path), volume["target"]))
    return found


def _runtime_mounts(config: dict, project_dir: Path) -> set[tuple[str, str, str, bool]]:
    """Every mount that is not a source mount: (service, target, source, read_only)."""
    source = _source_mounts(config, project_dir)
    mounts = set()
    for name, service in config["services"].items():
        for volume in service.get("volumes", []):
            path = _repository_path(volume.get("source", ""), project_dir)
            if volume["type"] == "bind" and (str(path), volume["target"]) in source.get(name, ()):
                continue
            mounts.add(
                (name, volume["target"], volume.get("source", ""), volume.get("read_only", False))
            )
    return mounts


@pytest.mark.parametrize("files", [PROD, STAND], ids=["prod", "stand"])
def test_no_prod_contour_container_mounts_repository_source(stacks, files):
    config, project_dir = stacks[files]

    assert _source_mounts(config, project_dir) == {}


@pytest.mark.parametrize("files", [PROD, STAND], ids=["prod", "stand"])
def test_every_runtime_mount_of_the_base_file_survives_on_the_prod_contour(stacks, files):
    """The overlay restates runtime mounts by hand; one it forgets is caught here."""
    base = _runtime_mounts(*stacks[BASE])
    contour = _runtime_mounts(*stacks[files])
    missing = {(service, target) for service, target, _source, _ro in base - contour}
    expected_missing = STAND_DROPS if files == STAND else set()

    assert missing == expected_missing


@pytest.mark.parametrize("files", [PROD, STAND], ids=["prod", "stand"])
def test_the_named_runtime_mounts_are_present_on_the_prod_contour(stacks, files):
    config, _project_dir = stacks[files]
    required = {
        (service, target)
        for service, targets in REQUIRED_RUNTIME_MOUNTS.items()
        for target in targets
    }
    if files == STAND:
        required -= STAND_DROPS
    present = {
        (name, volume["target"])
        for name, service in config["services"].items()
        for volume in service.get("volumes", [])
    }

    assert required - present == set()


def test_the_github_app_key_stays_read_only_in_production(stacks):
    config, _project_dir = stacks[PROD]
    for service in (
        name for name, targets in REQUIRED_RUNTIME_MOUNTS.items() if GITHUB_APP_KEY in targets
    ):
        (key,) = [
            volume
            for volume in config["services"][service]["volumes"]
            if volume["target"] == GITHUB_APP_KEY
        ]
        assert key["read_only"] is True, service


def test_development_keeps_its_source_mounts_and_local_builds(stacks):
    config, project_dir = stacks[BASE]
    mounted = _source_mounts(config, project_dir)

    for service, expected in DEV_SOURCE_MOUNTS.items():
        assert expected <= mounted.get(service, set()), service
        assert "build" in config["services"][service], service


def test_every_script_production_runs_in_the_api_container_is_in_its_image():
    """Production mounts no ./scripts over /app/scripts, so the image has to carry them.

    The callers are the deploy (config seeder), the prod reset (agent and system config
    seeders, `scripts/danger_prod_reset.py` builds the paths from a table), the stand e2e
    bring-up and the api entrypoint itself.
    """
    callers = [
        ".github/workflows/deploy.yml",
        ".github/workflows/stand-e2e.yml",
        "services/api/entrypoint.sh",
        "scripts/danger_prod_reset.py",
    ]
    invoked = set()
    for caller in callers:
        text = (ROOT / caller).read_text()
        invoked |= set(re.findall(r"/app/scripts/([\w.-]+\.(?:py|ya?ml))", text))
        # danger_prod_reset.py names each seeder and its config in a (script, config) table.
        for pair in re.findall(r'\("(seed_\w+\.py)", "(\w+\.ya?ml)"\)', text):
            invoked |= set(pair)
    dockerfile = (ROOT / "services/api/Dockerfile").read_text().replace("\\\n", " ")
    copied = set()
    for line in dockerfile.splitlines():
        tokens = line.split()
        if tokens[:1] == ["COPY"] and tokens[-1] == "/app/scripts/":
            copied |= {PurePosixPath(source).name for source in tokens[1:-1]}

    assert {"seed_system_configs.py", "system_configs.yaml", "seed_agent_configs.py"} <= invoked
    assert invoked - copied == set()
