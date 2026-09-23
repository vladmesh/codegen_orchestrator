"""The deploy runs the service release of one revision by digest, and builds nothing on the host.

Held down over the real `.github/workflows/deploy.yml`:

* one revision — the dispatched commit, or the `revision` input for a rollback — is
  validated first and used by every step: the runner checkout, both release waits, the
  host checkout, both pulls, the records and the target reconcile;
* a revision from before the service release was consumed is refused before the host;
* no step builds an image on the host, or prunes a build cache it no longer has;
* the worker and service releases are pulled and verified, concurrently, before the
  first step that changes a running container, and a failure of either fails the step
  with nothing written;
* every compose call from the pull on runs the release override, and every `up` runs
  with `--no-build --pull never`.

The steps that decide something are also rendered and run for real, against fake
`docker`, `git` and release consumers, so what is proved is behaviour and not wording.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
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
    checkout = _step("Check out deployed revision")["run"]
    assert f"git fetch --no-tags origin {REVISION}" in checkout
    assert f"git reset --hard {REVISION}" in checkout
    pull = _script(_step(PULL_STEP))
    assert f"WORKER_IMAGE_TAG='{REVISION}'" in pull
    assert f"SERVICE_IMAGE_TAG='{REVISION}'" in pull
    assert f'[ "${{deployed}}" != "{REVISION}" ]' in pull
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

    assert _run_bash(script, {"DEPLOY_REVISION": "x"}, REPO_ROOT).returncode == 0
    old_checkout = tmp_path / "checkout"
    (old_checkout / "scripts").mkdir(parents=True)
    (old_checkout / "scripts" / "wait_worker_release.py").write_text("")
    result = _run_bash(script, {"DEPLOY_REVISION": "x"}, old_checkout)
    assert result.returncode == 1
    assert "predates" in result.stderr


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


def test_both_releases_are_pulled_and_verified_before_any_container_changes():
    steps = _steps()
    names = _names()
    pull = names.index(PULL_STEP)
    changing = [i for i, step in enumerate(steps) if _changes_containers(step)]

    assert changing, "the deploy must change containers somewhere"
    assert pull < min(changing)
    assert names.index("Check out deployed revision") < pull
    script = _script(steps[pull])
    assert "bash infra/scripts/pull-worker-images.sh" in script
    assert "bash infra/scripts/pull-service-images.sh" in script
    assert not _changes_containers(steps[pull])


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


# --- the pull step, rendered and run -----------------------------------------------------

DEPLOYED = REAL_RECORD["git_sha"]
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

# Each consumer announces its start and then waits for the other's: run one after the
# other, the first would give up and the step would fail.
FAKE_WORKER_PULL = r"""#!/usr/bin/env bash
touch "${FAKE_HOST}/worker.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/service.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/service.started" ] || { echo "worker pull ran alone" >&2; exit 1; }
echo "{\"git_sha\": \"${WORKER_IMAGE_TAG}\"}" > "${DIGEST_FILE}"
exit "${FAKE_WORKER_EXIT:-0}"
"""
FAKE_SERVICE_PULL = r"""#!/usr/bin/env bash
touch "${FAKE_HOST}/service.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/worker.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/worker.started" ] || { echo "service pull ran alone" >&2; exit 1; }
[ -n "${PREVIOUS_DIGEST_FILE}" ] || exit 1
cp "${FAKE_HOST}/release.json" "${DIGEST_FILE}"
exit "${FAKE_SERVICE_EXIT:-0}"
"""
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "$*" >> "${FAKE_HOST}/docker.log"
case " $* " in
    *" pull "*) exit "${FAKE_THIRD_PARTY_EXIT:-0}" ;;
    *" config --format json "*) cat "${FAKE_HOST}/config.json" ;;
    *" config --quiet "*) exit 0 ;;
    *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""
FAKE_GIT = """#!/usr/bin/env bash
echo "${FAKE_HEAD}"
"""


def _render(script: str, deploy_path: Path) -> str:
    values = {
        "env.DEPLOY_PATH": str(deploy_path),
        "env.DEPLOY_REVISION": DEPLOYED,
        "env.COMPOSE_ARGS": "-f docker-compose.yml -f docker-compose.prod.yml",
        "env.SERVICE_RELEASE_COMPOSE": "deployed-service-images.compose.yml",
        "secrets.GHCR_TOKEN || github.token": "test-token",
        "github.repository_owner": "vladmesh",
    }
    return EXPRESSION.sub(lambda match: values[match.group(1)], script)


@pytest.fixture
def host(tmp_path: Path) -> Path:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True)
    (deploy / "infra" / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "service_release.py", deploy / "scripts")
    (deploy / "scripts" / "shared_freshness.py").write_text("print('tree-hash')\n")
    (deploy / "scripts" / "rotate_worker_image_records.py").write_text("")
    (deploy / "infra" / "scripts" / "pull-worker-images.sh").write_text(FAKE_WORKER_PULL)
    (deploy / "infra" / "scripts" / "pull-service-images.sh").write_text(FAKE_SERVICE_PULL)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("git", FAKE_GIT)):
        (binaries / name).write_text(body)
        (binaries / name).chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps(CONFIG))
    (tmp_path / "release.json").write_text(json.dumps(REAL_RECORD))
    (tmp_path / "docker.log").write_text("")
    return tmp_path


def _pull(host: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    script = _render(_script(_step(PULL_STEP)), host / "deploy")
    env = {
        "PATH": f"{host / 'bin'}:/usr/bin:/bin",
        "FAKE_HOST": str(host),
        "FAKE_HEAD": DEPLOYED,
        **overrides,
    }
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False, timeout=60
    )


def _override(host: Path) -> Path:
    return host / "deploy" / "deployed-service-images.compose.yml"


def test_both_releases_are_pulled_concurrently_and_then_compose_runs_the_record(host: Path):
    result = _pull(host)

    assert result.returncode == 0, result.stdout + result.stderr
    services = yaml.safe_load(_override(host).read_text())["services"]
    assert services == {
        "api": {"image": REAL_RECORD["images"]["api"]["reference"]},
        "architect": {"image": REAL_RECORD["images"]["langgraph"]["reference"]},
        "user-dashboard": {"image": REAL_RECORD["images"]["user-dashboard"]["reference"]},
    }
    docker = (host / "docker.log").read_text()
    assert "pull --ignore-buildable --policy missing --quiet" in docker
    assert "-f deployed-service-images.compose.yml config --quiet" in docker


@pytest.mark.parametrize(
    "failure",
    [{"FAKE_WORKER_EXIT": "5"}, {"FAKE_SERVICE_EXIT": "9"}, {"FAKE_THIRD_PARTY_EXIT": "1"}],
)
def test_a_failure_of_either_pull_fails_the_step_before_compose_is_pointed_anywhere(
    host: Path, failure: dict[str, str]
):
    result = _pull(host, **failure)

    assert result.returncode == 1
    assert "pulling the release failed" in result.stderr
    assert not _override(host).exists()
    assert "config --format json" not in (host / "docker.log").read_text()


def test_a_host_at_another_revision_is_refused_before_anything_is_pulled(host: Path):
    result = _pull(host, FAKE_HEAD="0" * 40)

    assert result.returncode == 1
    assert not (host / "worker.started").exists()
    assert not (host / "service.started").exists()
