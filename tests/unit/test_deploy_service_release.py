"""The deploy runs the service release of one revision by digest, and builds nothing on the host.

Held down over the real `.github/workflows/deploy.yml`:

* one revision — the dispatched commit, or the `revision` input for a rollback — is
  validated first and used by every step: the runner checkout, both release waits, the
  host checkout, both pulls, the records and the target reconcile;
* a revision from before the service release was consumed is refused before the host;
* no step builds an image on the host, or prunes a build cache it no longer has;
* one boundary, the first step that touches what a running container sees: before it,
  the host is written only in a staged worktree of the revision outside the deploy path
  and in files no container mounts; the worker and service releases are pulled and
  verified there, concurrently, and a failure of either fails the step with nothing
  moved; after it come, adjacent and in this order, the bind-mounted secret files, the
  reset of the bind-mounted source tree, and `up`;
* every compose call from the pull on runs the release override, and every `up` runs
  with `--no-build --pull never`.

The steps that decide something are also rendered and run for real, against fake
`docker`, `git` and release consumers, so what is proved is behaviour and not wording.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
REAL_RECORD = json.loads(
    (Path(__file__).parent / "fixtures" / "service-release-7f93d8b7.json").read_text()
)
REVISION = "${{ env.DEPLOY_REVISION }}"
OVERRIDE = "-f ${{ env.SERVICE_RELEASE_COMPOSE }}"
PULL_STEP = "Pull and verify this revision's worker and service releases"
VALIDATE_STEP = "Validate the deployed revision"
PREDATES_STEP = "Refuse a revision that predates pulled service releases"
BOUNDARY_STEPS = ("Write secrets files to server", "Check out deployed revision", "Deploy")
WAIT_STEP = "Wait for this revision's worker and service releases"
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
CHANGING_SUBCOMMANDS = ("up", "down", "restart", "exec", "run", "stop", "start", "rm", "kill")


def _workflow() -> dict:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text())
    workflow["on"] = workflow.get("on", workflow.get(True))
    return workflow


def _job() -> dict:
    return _workflow()["jobs"]["deploy"]


def _steps() -> list[dict]:
    return _job()["steps"]


def _names() -> list[str]:
    return [step["name"] for step in _steps()]


def _step(name: str) -> dict:
    return next(step for step in _steps() if step["name"] == name)


def _script(step: dict) -> str:
    return step.get("run") or step.get("with", {}).get("script") or ""


def _compose_calls(script: str) -> list[str]:
    """Every `docker compose` invocation of a script, continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [" ".join(line.split()) for line in joined.splitlines() if "docker compose" in line]


def _subcommand(call: str) -> str:
    """The compose subcommand of one invocation: the first word that is not an option."""
    words = EXPRESSION.sub("EXPRESSION", call).split("docker compose", 1)[1].split()
    index = 0
    while index < len(words):
        if words[index] == "-f":
            index += 2
        elif words[index] == "EXPRESSION" or words[index].startswith("-"):
            index += 1
        else:
            return words[index]
    raise AssertionError(f"no subcommand in {call!r}")


def _touches_host(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("appleboy/ssh-action") or (
        "infra/scripts/deploy-ssh.sh" in (step.get("run") or "")
    )


def _changes_containers(step: dict) -> bool:
    return any(_subcommand(call) in CHANGING_SUBCOMMANDS for call in _compose_calls(_script(step)))


# --- one revision ----------------------------------------------------------------------


def test_the_revision_is_an_optional_input_that_defaults_to_the_dispatched_commit():
    revision = _workflow()["on"]["workflow_dispatch"]["inputs"]["revision"]

    assert revision["required"] is False
    assert revision["default"] == ""
    assert revision["type"] == "string"
    assert _job()["env"]["DEPLOY_REVISION"] == "${{ inputs.revision || github.sha }}"


def test_no_step_reads_the_dispatched_commit_instead_of_the_deployed_revision():
    """`github.sha` is read once, as the default of DEPLOY_REVISION, and nowhere else."""
    offenders = [step["name"] for step in _steps() if "github.sha" in yaml.dump(step)]

    assert not offenders, f"steps not using the one deployed revision: {offenders}"


def test_every_step_that_names_a_revision_names_the_deployed_one():
    assert _step("Checkout code")["with"]["ref"] == REVISION
    assert '--revision "${DEPLOY_REVISION}"' in _step(WAIT_STEP)["run"]
    assert f"git reset --hard {REVISION}" in _step("Check out deployed revision")["run"]
    pull = _script(_step(PULL_STEP))
    assert f'git -C "${{live}}" fetch --no-tags origin {REVISION}' in pull
    assert f'git -C "${{live}}" worktree add --detach "${{stage}}" {REVISION}' in pull
    assert f"WORKER_IMAGE_TAG='{REVISION}'" in pull
    assert f"SERVICE_IMAGE_TAG='{REVISION}'" in pull
    assert f"--revision {REVISION}" in _script(_step("Reconcile managed deploy targets"))
    for upload in ("Upload deployed worker image digests", "Upload deployed service image digests"):
        assert _step(upload)["with"]["name"].endswith(f"-{REVISION}")


def _run_bash(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_revision_is_validated_before_anything_reads_it(tmp_path):
    names = _names()
    assert names[0] == VALIDATE_STEP
    script = _step(VALIDATE_STEP)["run"]

    assert _run_bash(script, {"DEPLOY_REVISION": REAL_RECORD["git_sha"]}, tmp_path).returncode == 0
    for refused in ("main", REAL_RECORD["git_sha"][:12], "", f"$(touch {tmp_path}/pwned)"):
        result = _run_bash(script, {"DEPLOY_REVISION": refused}, tmp_path)
        assert result.returncode == 1, refused
    assert not (tmp_path / "pwned").exists()


def test_a_revision_that_predates_the_service_release_pull_is_refused_before_the_host(tmp_path):
    names = _names()
    steps = _steps()
    first_host_step = min(i for i, step in enumerate(steps) if _touches_host(step))
    assert names.index("Checkout code") < names.index(PREDATES_STEP) < names.index(WAIT_STEP)
    assert names.index(WAIT_STEP) < first_host_step
    script = _step(PREDATES_STEP)["run"]
    tooling = _job()["env"]["RELEASE_TOOLING"]
    env = {"DEPLOY_REVISION": "x", "RELEASE_TOOLING": tooling}

    assert "infra/scripts/pull-service-images.sh" in tooling.split()
    assert _run_bash(script, env, REPO_ROOT).returncode == 0
    old_checkout = tmp_path / "checkout"
    (old_checkout / "scripts").mkdir(parents=True)
    (old_checkout / "scripts" / "wait_worker_release.py").write_text("")
    result = _run_bash(script, env, old_checkout)
    assert result.returncode == 1
    assert "predates" in result.stderr
    # The host checks the revision it staged against the same list.
    assert "for path in ${{ env.RELEASE_TOOLING }}; do" in _script(_step(PULL_STEP))


# --- no build on the host --------------------------------------------------------------


def test_no_step_builds_an_image_on_the_host():
    for step in _steps():
        script = _script(step)
        for call in _compose_calls(script):
            assert _subcommand(call) != "build", f"{step['name']} builds: {call}"
            assert "--build" not in call.split(), f"{step['name']} builds: {call}"
        assert "docker build" not in script, step["name"]
        assert "builder prune" not in script, step["name"]
    assert "Build service images" not in _names()


def test_every_up_runs_the_release_by_digest_and_pulls_nothing():
    ups = [
        call
        for step in _steps()
        for call in _compose_calls(_script(step))
        if _subcommand(call) == "up"
    ]

    assert len(ups) == 2, ups
    for call in ups:
        assert OVERRIDE in call, call
        assert "--no-build" in call.split(), call
        assert "--pull never" in call, call


def test_every_compose_call_after_the_pull_runs_the_release():
    names = _names()
    pull = names.index(PULL_STEP)
    for step in _steps()[pull + 1 :]:
        for call in _compose_calls(_script(step)):
            assert OVERRIDE in call, f"{step['name']} runs compose without the release: {call}"


def _boundary() -> int:
    """The index of the first step that touches what a running container sees."""
    return _names().index(BOUNDARY_STEPS[0])


def test_nothing_before_the_boundary_touches_what_a_running_container_sees():
    """Mounted secrets, the mounted source tree, and the containers themselves.

    Before the boundary a step may write to the runner, to the staged worktree, and to
    files no container mounts. So none of them writes /opt/secrets, moves the live tree
    (the one git operations allowed there write only into .git: a fetch and the stage's
    worktree), or runs a compose subcommand that changes a container.
    """
    for step in _steps()[: _boundary()]:
        script = _script(step)
        # .env names the key's path as a value; writing there is what is refused.
        writes = re.findall(
            r"(?:>|\b(?:mkdir|chown|chmod|cp|mv|install|tee)\b)[^\n]*/opt/secrets", script
        )
        assert not writes, f"{step['name']} writes the mounted secrets: {writes}"
        for command in ("git reset", "git checkout", "git pull", "git clean", "git merge"):
            assert command not in script, f"{step['name']} moves the live tree: {command}"
        assert not _changes_containers(step), step["name"]
    pull = _script(_step(PULL_STEP))
    git_calls = re.findall(r"\bgit\s+-C\s+\"\$\{live\}\"\s+(\w+)", pull)
    assert set(git_calls) == {"fetch", "worktree"}, git_calls


def test_the_switch_follows_every_pull_and_check_adjacent_and_in_order():
    names = _names()
    steps = _steps()
    boundary = _boundary()

    assert names[boundary : boundary + 3] == list(BOUNDARY_STEPS)
    assert names.index(PULL_STEP) < boundary
    assert names.index(WAIT_STEP) < boundary
    assert all(
        names.index(step) < boundary
        for step in names
        if "pull-" in _script(_step(step)) or "compose-override" in _script(_step(step))
    )
    changing = [i for i, step in enumerate(steps) if _changes_containers(step)]
    assert min(changing) == names.index("Deploy")
    secrets, checkout, deploy = (_script(_step(name)) for name in BOUNDARY_STEPS)
    assert "/opt/secrets/github_app.pem" in secrets
    assert f"git reset --hard {REVISION}" in checkout
    up = deploy.index("up -d --remove-orphans --no-build --pull never")
    assert up < deploy.index(
        "bash infra/scripts/retag-worker-images.sh deployed-worker-images.json"
    )


def test_the_worker_release_is_verified_before_the_boundary_and_retagged_after_it():
    pull = _script(_step(PULL_STEP))

    assert "bash infra/scripts/pull-worker-images.sh" in pull
    assert "bash infra/scripts/pull-service-images.sh" in pull
    assert "RELEASE_DEFER_RETAG=true" in pull
    for step in _steps()[: _boundary()]:
        assert "retag-worker-images.sh" not in _script(step), step["name"]


def test_migrations_and_the_seeder_run_in_the_released_api_container():
    for name, command in (
        ("Run migrations", "exec -T api alembic upgrade head"),
        ("Apply system configs", "exec -T api python /app/scripts/seed_system_configs.py"),
    ):
        calls = _compose_calls(_script(_step(name)))
        assert len(calls) == 1
        assert OVERRIDE in calls[0]
        assert command in calls[0]
    names = _names()
    assert names.index("Deploy") < names.index("Run migrations")


def test_cleanup_is_last_and_bounded():
    cleanup = _steps()[-1]

    assert cleanup["name"] == "Cleanup"
    assert cleanup["timeout-minutes"] <= 10
    assert "service_release.py cleanup" in _script(cleanup)
    assert "previous-deployed-service-images.json" in _script(cleanup)


# --- the pull step, rendered and run against a real git deploy path -----------------------

CONFIG = {
    "services": {
        "api": {"image": "codegen-orchestrator/api:local", "build": {"context": "."}},
        "architect": {"image": "codegen-orchestrator/langgraph:local", "build": {"context": "."}},
        "user-dashboard": {
            "image": "codegen-orchestrator/user-dashboard:local",
            "build": {"context": "services/user-dashboard"},
        },
        "redis": {"image": "redis:7.4.10-alpine"},
    }
}
RECORDS = (
    "deployed-worker-images.json",
    "previous-deployed-worker-images.json",
    "deployed-service-images.json",
    "previous-deployed-service-images.json",
)

# Each consumer announces its start and then waits for the other's: run one after the
# other, the first would give up and the step would fail. Both check they run from the
# stage — the revision's tree, not the live one — and write only where they are told.
FAKE_WORKER_PULL = r"""#!/usr/bin/env bash
[ "${RELEASE_DEFER_RETAG:-}" = true ] || { echo "live worker tags would move" >&2; exit 1; }
[ "$(cat shared/marker.txt)" = new ] || { echo "not run from the stage" >&2; exit 1; }
touch "${FAKE_HOST}/worker.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/service.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/service.started" ] || { echo "worker pull ran alone" >&2; exit 1; }
echo '{"git_sha": "worker"}' > "${DIGEST_FILE}"
exit "${FAKE_WORKER_EXIT:-0}"
"""
FAKE_SERVICE_PULL = r"""#!/usr/bin/env bash
[ "$(cat shared/marker.txt)" = new ] || { echo "not run from the stage" >&2; exit 1; }
touch "${FAKE_HOST}/service.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/worker.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/worker.started" ] || { echo "service pull ran alone" >&2; exit 1; }
[ -n "${PREVIOUS_DIGEST_FILE}" ] || exit 1
if [ -f "${DIGEST_FILE}" ]; then cp "${DIGEST_FILE}" "${PREVIOUS_DIGEST_FILE}"; fi
cp "${FAKE_HOST}/release.json" "${DIGEST_FILE}"
exit "${FAKE_SERVICE_EXIT:-0}"
"""
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$(pwd) $*" >> "${FAKE_HOST}/docker.log"
[ -f .env ] || { echo "compose has no .env here" >&2; exit 1; }
case " $* " in
    *" pull "*) exit "${FAKE_THIRD_PARTY_EXIT:-0}" ;;
    *" config --format json "*) cat "${FAKE_HOST}/config.json" ;;
    *" config --quiet "*) exit "${FAKE_CONFIG_EXIT:-0}" ;;
    *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env={"PATH": "/usr/bin:/bin", **GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class DeployHost:
    """A deploy path running revision `old`, asked to deploy `new`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.live = root / "live"
        self.home = root / "home"
        self.home.mkdir()
        files = {
            "infra/scripts/pull-worker-images.sh": FAKE_WORKER_PULL,
            "infra/scripts/pull-service-images.sh": FAKE_SERVICE_PULL,
            "infra/scripts/retag-worker-images.sh": "",
            "scripts/wait_release.py": "",
            "scripts/shared_freshness.py": "print('tree-hash')\n",
            "scripts/service_release.py": (
                REPO_ROOT / "scripts" / "service_release.py"
            ).read_text(),
            "scripts/rotate_worker_image_records.py": (
                REPO_ROOT / "scripts" / "rotate_worker_image_records.py"
            ).read_text(),
            "scripts/cleanup_worker_images.py": (
                REPO_ROOT / "scripts" / "cleanup_worker_images.py"
            ).read_text(),
            "shared/marker.txt": "old\n",
            ".gitignore": ".env\n*.json\n*.compose.yml\n",
        }
        for name, body in files.items():
            (self.live / name).parent.mkdir(parents=True, exist_ok=True)
            (self.live / name).write_text(body)
        _git(self.live, "init", "-q")
        _git(self.live, "add", "-A")
        _git(self.live, "commit", "-qm", "old")
        self.old = _git(self.live, "rev-parse", "HEAD")
        (self.live / "shared" / "marker.txt").write_text("new\n")
        _git(self.live, "commit", "-qam", "new")
        self.new = _git(self.live, "rev-parse", "HEAD")
        _git(self.live, "reset", "-q", "--hard", self.old)
        _git(self.live, "remote", "add", "origin", str(self.live))
        (self.live / ".env").write_text("POSTGRES_DB=x\n")
        # What the running deployment recorded, and must keep until the switch.
        for record in RECORDS:
            (self.live / record).write_text(f'{{"running": "{record}"}}\n')
        binaries = root / "bin"
        binaries.mkdir()
        (binaries / "docker").write_text(FAKE_DOCKER)
        (binaries / "docker").chmod(0o755)
        (root / "config.json").write_text(json.dumps(CONFIG))
        (root / "release.json").write_text(json.dumps(REAL_RECORD))
        (root / "docker.log").write_text("")

    @property
    def stage(self) -> Path:
        return self.home / ".stage"

    def pull(self, revision: str | None = None, **overrides: str):
        values = {
            "env.DEPLOY_PATH": str(self.live),
            "env.DEPLOY_REVISION": revision or self.new,
            "env.RELEASE_STAGE": ".stage",
            "env.RELEASE_TOOLING": _job()["env"]["RELEASE_TOOLING"],
            "env.COMPOSE_ARGS": "-f docker-compose.yml -f docker-compose.prod.yml",
            "env.SERVICE_RELEASE_COMPOSE": "deployed-service-images.compose.yml",
            "secrets.GHCR_TOKEN || github.token": "test-token",
            "github.repository_owner": "vladmesh",
        }
        script = EXPRESSION.sub(lambda m: values[m.group(1)], _script(_step(PULL_STEP)))
        env = {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_HOST": str(self.root),
            **GIT_ENV,
            **overrides,
        }
        return subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60
        )

    def assert_live_tree_untouched(self) -> None:
        assert _git(self.live, "rev-parse", "HEAD") == self.old
        assert (self.live / "shared" / "marker.txt").read_text() == "old\n"
        assert _git(self.live, "status", "--porcelain", "--untracked-files=no") == ""
        assert not self.stage.exists(), "the stage is removed however the step ends"
        assert _git(self.live, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.fixture
def deploy_host(tmp_path: Path) -> DeployHost:
    return DeployHost(tmp_path)


def _override(host: DeployHost) -> Path:
    return host.live / "deployed-service-images.compose.yml"


def test_the_revision_is_verified_from_a_stage_and_only_the_records_reach_the_live_path(
    deploy_host: DeployHost,
):
    result = deploy_host.pull()

    assert result.returncode == 0, result.stdout + result.stderr
    deploy_host.assert_live_tree_untouched()
    services = yaml.safe_load(_override(deploy_host).read_text())["services"]
    assert services == {
        "api": {"image": REAL_RECORD["images"]["api"]["reference"]},
        "architect": {"image": REAL_RECORD["images"]["langgraph"]["reference"]},
        "user-dashboard": {"image": REAL_RECORD["images"]["user-dashboard"]["reference"]},
    }
    live = deploy_host.live
    assert json.loads((live / "deployed-service-images.json").read_text()) == REAL_RECORD
    assert (live / "previous-deployed-service-images.json").read_text() == (
        '{"running": "deployed-service-images.json"}\n'
    )
    assert json.loads((live / "deployed-worker-images.json").read_text()) == {"git_sha": "worker"}
    docker = (deploy_host.root / "docker.log").read_text().splitlines()
    assert docker, "compose ran"
    assert all(line.startswith(str(deploy_host.stage)) for line in docker), docker
    assert any("pull --ignore-buildable --policy missing --quiet" in line for line in docker)
    assert any(
        ".release-out/deployed-service-images.compose.yml config --quiet" in line for line in docker
    )


@pytest.mark.parametrize(
    "failure",
    [
        {"FAKE_WORKER_EXIT": "5"},
        {"FAKE_SERVICE_EXIT": "9"},
        {"FAKE_THIRD_PARTY_EXIT": "1"},
        {"FAKE_CONFIG_EXIT": "1"},
    ],
)
def test_a_refusal_leaves_the_live_path_exactly_as_it_was(
    deploy_host: DeployHost, failure: dict[str, str]
):
    before = {record: (deploy_host.live / record).read_text() for record in RECORDS}

    result = deploy_host.pull(**failure)

    assert result.returncode != 0
    deploy_host.assert_live_tree_untouched()
    assert not _override(deploy_host).exists()
    assert {record: (deploy_host.live / record).read_text() for record in RECORDS} == before


def test_a_stage_left_by_a_crashed_run_is_replaced(deploy_host: DeployHost):
    deploy_host.stage.mkdir()
    (deploy_host.stage / "leftover").write_text("from a run that died\n")

    result = deploy_host.pull()

    assert result.returncode == 0, result.stdout + result.stderr
    deploy_host.assert_live_tree_untouched()


def test_a_revision_without_the_release_tooling_is_refused_on_the_host_before_any_pull(
    deploy_host: DeployHost,
):
    (deploy_host.live / "scripts" / "service_release.py").unlink()
    _git(deploy_host.live, "commit", "-qam", "predates")
    predates = _git(deploy_host.live, "rev-parse", "HEAD")
    _git(deploy_host.live, "reset", "-q", "--hard", deploy_host.old)

    result = deploy_host.pull(revision=predates)

    assert result.returncode == 1
    assert "predates pulled service releases" in result.stderr
    assert not (deploy_host.root / "worker.started").exists()
    assert not (deploy_host.root / "service.started").exists()
    deploy_host.assert_live_tree_untouched()
