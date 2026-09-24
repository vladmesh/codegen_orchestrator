"""The deploy runs the service release of one revision by digest, and builds nothing on the host.

Held down over the real `.github/workflows/deploy.yml`:

* one revision — the dispatched commit, or the `revision` input for a rollback — is
  validated first and used by every step: the runner checkout, both release waits, the
  host checkout, both pulls, the records and the target reconcile;
* a revision from before the service release was consumed is refused before the host;
* no step builds an image on the host, or prunes a build cache it no longer has;
* live host state — the SECRETS_PATH files, the deploy path's tracked checkout, the
  worker-base-*:latest tags, the running compose project, the deployed/previous release
  records and the compose override `up` reads — changes in exactly one step, `Switch`;
* before it, the releases are pulled and verified concurrently from a staged worktree
  outside the deploy path, and what passed becomes a pending set that no container mounts
  (a stale one is discarded first); a refusal leaves every live thing as it was;
* inside it, in this order: check the pending set, the secret file, the checkout reset,
  the worker retag (before `up`), the override and `up`, and only then the promotion of
  the records, which rotates the live records and never the pending ones;
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
SWITCH_STEP = "Switch"
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
    assert f"git reset --hard {REVISION}" in _step(SWITCH_STEP)["run"]
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


LIVE_RECORDS = (
    "deployed-worker-images.json",
    "deployed-service-images.json",
    "previous-deployed-worker-images.json",
    "previous-deployed-service-images.json",
    "deployed-service-images.compose.yml",
)
# A write outside the Switch is allowed only into these: the verify step's staged
# output, the pending set it builds, the runner's own files.
NOT_LIVE = ("${out}", "${pending}.next", "${pending}", "${RECORDS}", "_DIGEST_FILE}", ".env")


def _write_targets(script: str) -> list[str]:
    """What a script writes: redirect targets, and the destination of mv, cp and install."""
    joined = re.sub(r"\\\n\s*", " ", script)
    targets = re.findall(r"(?<![0-9&])>>?\s*(\S+)", joined)
    for line in joined.splitlines():
        words = line.split()
        if words and words[0] in ("mv", "cp", "install"):
            targets.append(words[-1])
    targets += re.findall(r"\bDIGEST_FILE=(\S+)", joined)
    return [target.strip("\"'") for target in targets if target != "/dev/null"]


def _writes_live_state(script: str) -> list[str]:
    """Every write in a script to a thing the Switch alone may change."""
    found = re.findall(
        r"(?:>|\b(?:mkdir|chown|chmod|cp|mv|install|tee)\b)[^\n]*"
        r"(?:/opt/secrets|\$\{\{ env\.SECRETS_PATH \}\})",
        script,
    )
    for command in ("git reset", "git checkout", "git pull", "git clean", "git merge"):
        if command in script:
            found.append(command)
    for pattern in (r"retag-worker-images\.sh", r"docker tag\b", r"release_switch\.py promote"):
        found += re.findall(pattern, script)
    if "pull-worker-images.sh" in script and "RELEASE_DEFER_RETAG=true" not in script:
        found.append("pull-worker-images.sh moves worker-base-*:latest")
    for call in _compose_calls(script):
        if _subcommand(call) in CHANGING_SUBCOMMANDS and _subcommand(call) != "exec":
            found.append(call)
    for target in _write_targets(script):
        if any(name in target for name in LIVE_RECORDS) and not target.startswith(NOT_LIVE):
            found.append(target)
    return found


def test_live_host_state_is_written_only_by_the_switch():
    """Secrets, checkout, :latest tags, the compose project, the records and override."""
    for step in _steps():
        if step["name"] == SWITCH_STEP:
            continue
        assert not _writes_live_state(_script(step)), step["name"]
    assert _writes_live_state(_script(_step(SWITCH_STEP))), "the detector sees the Switch"
    pull = _script(_step(PULL_STEP))
    git_calls = re.findall(r"\bgit\s+-C\s+\"\$\{live\}\"\s+(\w+)", pull)
    assert set(git_calls) == {"fetch", "worktree"}, "the verify step writes .git only"


def test_the_switch_changes_live_state_in_the_decided_order():
    names = _names()
    switch = _script(_step(SWITCH_STEP))

    assert names.index(PULL_STEP) < names.index(SWITCH_STEP)
    order = [
        switch.index('release_switch.py" check --pending "${pending}"'),
        switch.index("> ${{ env.SECRETS_PATH }}/github_app.pem"),
        switch.index(f"git reset --hard {REVISION}"),
        switch.index('retag-worker-images.sh "${pending}/deployed-worker-images.json"'),
        switch.index('mv -f "${{ env.SERVICE_RELEASE_COMPOSE }}.next"'),
        switch.index("up -d --remove-orphans --no-build --pull never"),
        switch.index("release_switch.py promote"),
        switch.index("exec -T api alembic upgrade head"),
        switch.index("seed_system_configs.py"),
        switch.index("up -d --force-recreate --no-deps --wait"),
    ]
    assert order == sorted(order), order
    assert switch.count("release_switch.py") == 2, "one check, one promotion, nothing else"


def test_the_switch_script_is_read_whole_before_it_runs():
    """`bash -s` reads it from stdin; `compose exec` must not eat the rest of it."""
    remote = _switch_remote()
    lines = [line for line in remote.splitlines() if line.strip()]

    assert lines[0] == "set -euo pipefail"
    first = next(i for i, line in enumerate(lines) if not line.startswith("#") and i > 0)
    assert lines[first] == "{"
    assert lines[-1] == "} < /dev/null"


def test_promotion_rotates_the_live_records_and_never_the_pending_ones():
    switch = _script(_step(SWITCH_STEP))

    promote = re.search(r"release_switch\.py promote (.*?)\n\s*\n", switch, re.S)
    assert promote, switch
    assert '--pending "${pending}" --live "${live}"' in promote.group(1)
    for step in _steps():
        script = _script(step)
        assert "rotate_worker_image_records.py" not in script, step["name"]
        assert "service_release.py rotate" not in script, step["name"]


def test_the_worker_release_is_verified_before_the_switch_and_retagged_in_it_before_up():
    pull = _script(_step(PULL_STEP))
    switch = _script(_step(SWITCH_STEP))

    assert "bash infra/scripts/pull-worker-images.sh" in pull
    assert "bash infra/scripts/pull-service-images.sh" in pull
    assert "RELEASE_DEFER_RETAG=true" in pull
    assert switch.index("retag-worker-images.sh") < switch.index("up -d --remove-orphans")


def test_migrations_and_the_seeder_run_in_the_released_api_container():
    switch = _script(_step(SWITCH_STEP))
    calls = _compose_calls(switch)

    for command in (
        "exec -T api alembic upgrade head",
        "exec -T api python /app/scripts/seed_system_configs.py",
    ):
        matching = [call for call in calls if command in call]
        assert len(matching) == 1, command
        assert OVERRIDE in matching[0]
    assert switch.index("up -d --remove-orphans") < switch.index("alembic upgrade head")


def test_the_switch_proves_the_api_reads_the_github_app_key_before_seeding():
    """A layout where HOST_UID cannot read the 0600 key fails the deploy, not the first
    GitHub call after it."""
    switch = _script(_step(SWITCH_STEP))
    calls = _compose_calls(switch)

    proof = [call for call in calls if "/app/keys/github_app.pem" in call]
    assert len(proof) == 1
    assert "exec -T api" in proof[0]
    assert OVERRIDE in proof[0]
    assert switch.index("API is healthy") < switch.index("/app/keys/github_app.pem")
    assert switch.index("/app/keys/github_app.pem") < switch.index("seed_system_configs.py")


def test_cleanup_is_last_and_bounded():
    cleanup = _steps()[-1]

    assert cleanup["name"] == "Cleanup"
    assert cleanup["timeout-minutes"] <= 10
    assert "service_release.py cleanup" in _script(cleanup)
    assert "previous-deployed-service-images.json" in _script(cleanup)


# --- the verify step and the Switch, rendered and run against a real git deploy path -------

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
PENDING = ".release-pending"

# Each consumer announces its start and then waits for the other's: run one after the
# other, the first would give up and the step would fail. Both check they run from the
# stage — the revision's tree, not the live one — and write only where they are told:
# the release of the revision being deployed, with the real record's source hash.
FAKE_WORKER_PULL = r"""#!/usr/bin/env bash
[ "${RELEASE_DEFER_RETAG:-}" = true ] || { echo "live worker tags would move" >&2; exit 1; }
[ "$(cat shared/marker.txt)" = new ] || { echo "not run from the stage" >&2; exit 1; }
touch "${FAKE_HOST}/worker.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/service.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/service.started" ] || { echo "worker pull ran alone" >&2; exit 1; }
printf '{"git_sha": "%s", "source_hash": "%s", "images": {}}\n' \
    "${WORKER_IMAGE_TAG}" "${FAKE_SOURCE_HASH}" > "${DIGEST_FILE}"
exit "${FAKE_WORKER_EXIT:-0}"
"""
FAKE_SERVICE_PULL = r"""#!/usr/bin/env bash
[ "$(cat shared/marker.txt)" = new ] || { echo "not run from the stage" >&2; exit 1; }
touch "${FAKE_HOST}/service.started"
for _ in $(seq 50); do [ -f "${FAKE_HOST}/worker.started" ] && break; sleep 0.1; done
[ -f "${FAKE_HOST}/worker.started" ] || { echo "service pull ran alone" >&2; exit 1; }
sed "s/${FAKE_RELEASED_SHA}/${SERVICE_IMAGE_TAG}/" "${FAKE_HOST}/release.json" > "${DIGEST_FILE}"
exit "${FAKE_SERVICE_EXIT:-0}"
"""
# Everything the Switch does to the host goes to one log, in order. `exec` reads its
# stdin, as `docker compose exec` does: run ungrouped under `bash -s`, it would swallow
# the rest of the script.
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "${FAKE_HOST}/host.log"
[ -f .env ] || { echo "compose has no .env here" >&2; exit 1; }
case " $* " in
    *" exec "*) cat > /dev/null ;;
    *" up -d --remove-orphans "*) exit "${FAKE_UP_EXIT:-0}" ;;
    *" up "*) exit 0 ;;
    *" pull "*) exit "${FAKE_THIRD_PARTY_EXIT:-0}" ;;
    *" config --format json "*) cat "${FAKE_HOST}/config.json" ;;
    *" config --quiet "*) exit "${FAKE_CONFIG_EXIT:-0}" ;;
    *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""
FAKE_RETAG = r"""#!/usr/bin/env bash
echo "retag $1" >> "${FAKE_HOST}/host.log"
"""
FAKE_SUDO = r"""#!/usr/bin/env bash
[ "$1" = chown ] && exit 0
exec "$@"
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


WORKER_CHAIN = (
    "worker-base-common",
    "worker-base-claude",
    "worker-base-factory",
    "worker-base-codex",
)


def _running_record(chain: str, git_sha: str) -> str:
    """A valid deployed record of `git_sha`, as a previous deploy promoted it."""
    if chain == "service":
        record = dict(REAL_RECORD, git_sha=git_sha, source_hash=f"hash-{git_sha[:6]}")
    else:
        record = {
            "git_sha": git_sha,
            "source_hash": f"hash-{git_sha[:6]}",
            "images": {
                name: {
                    "reference": f"ghcr.io/vladmesh/codegen-orchestrator/{name}@sha256:{git_sha}"
                }
                for name in WORKER_CHAIN
            },
        }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _switch_remote() -> str:
    """The script the Switch sends to the host: its deploy-ssh.sh heredoc."""
    run = _step(SWITCH_STEP)["run"]
    body = run.split("<<'REMOTE'\n", 1)[1]
    return body[: body.rindex("REMOTE")]


class DeployHost:
    """A deploy path running revision `old`, asked to deploy `new`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.live = root / "live"
        self.home = root / "home"
        self.home.mkdir()
        self.secrets = root / "secrets"
        files = {
            "infra/scripts/pull-worker-images.sh": FAKE_WORKER_PULL,
            "infra/scripts/pull-service-images.sh": FAKE_SERVICE_PULL,
            "infra/scripts/retag-worker-images.sh": FAKE_RETAG,
            "scripts/wait_release.py": "",
            "scripts/shared_freshness.py": "print('tree-hash')\n",
            "scripts/release_switch.py": (REPO_ROOT / "scripts" / "release_switch.py").read_text(),
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
            ".gitignore": ".env\n*.json\n*.compose.yml\n*.next\n.release-pending/\n",
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
        # What the running deployment recorded, and must keep until a promotion: valid
        # records of the old revision (and of an older one, as previous).
        for record, git_sha in zip(RECORDS, (self.old, "a" * 40, self.old, "b" * 40), strict=True):
            chain = "worker" if "worker" in record else "service"
            (self.live / record).write_text(_running_record(chain, git_sha))
        binaries = root / "bin"
        binaries.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("sudo", FAKE_SUDO)):
            (binaries / name).write_text(body)
            (binaries / name).chmod(0o755)
        (root / "config.json").write_text(json.dumps(CONFIG))
        (root / "release.json").write_text(json.dumps(REAL_RECORD))
        (root / "host.log").write_text("")

    @property
    def stage(self) -> Path:
        return self.home / ".stage"

    @property
    def pending(self) -> Path:
        return self.live / PENDING

    def _values(self, revision: str) -> dict[str, str]:
        return {
            "env.DEPLOY_PATH": str(self.live),
            "env.DEPLOY_REVISION": revision,
            "env.RELEASE_STAGE": ".stage",
            "env.RELEASE_PENDING": PENDING,
            "env.RELEASE_TOOLING": _job()["env"]["RELEASE_TOOLING"],
            "env.COMPOSE_ARGS": "-f docker-compose.yml -f docker-compose.prod.yml",
            "env.SERVICE_RELEASE_COMPOSE": "deployed-service-images.compose.yml",
            "env.SECRETS_PATH": str(self.secrets),
            "env.DEPLOY_SSH_USER": "deploy",
            "env.HOST_UID": "1000",
            "env.HOST_GID": "1000",
            "secrets.GHCR_TOKEN || github.token": "test-token",
            "secrets.GH_APP_PRIVATE_KEY": "the-new-app-key",
            "github.repository_owner": "vladmesh",
        }

    def _run(self, script: str, revision: str, stdin: str | None = None, **overrides: str):
        values = self._values(revision)
        rendered = EXPRESSION.sub(lambda m: values[m.group(1)], script)
        env = {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_HOST": str(self.root),
            "FAKE_RELEASED_SHA": REAL_RECORD["git_sha"],
            "FAKE_SOURCE_HASH": REAL_RECORD["source_hash"],
            **GIT_ENV,
            **overrides,
        }
        command = ["bash", "-s"] if stdin is None else ["bash", "-c", rendered]
        return subprocess.run(
            command,
            input=rendered if stdin is None else stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def verify(self, revision: str | None = None, **overrides: str):
        """The verify step, as appleboy runs it: a command, stdin unused."""
        return self._run(_script(_step(PULL_STEP)), revision or self.new, stdin="", **overrides)

    def switch(self, revision: str | None = None, **overrides: str):
        """The Switch, as deploy-ssh.sh runs it: `bash -s` reading the script on stdin."""
        return self._run(_switch_remote(), revision or self.new, **overrides)

    def records(self) -> dict[str, str]:
        return {record: (self.live / record).read_text() for record in RECORDS}

    def host_log(self) -> list[str]:
        return (self.root / "host.log").read_text().splitlines()

    def assert_nothing_live_changed(self, records: dict[str, str]) -> None:
        assert _git(self.live, "rev-parse", "HEAD") == self.old
        assert (self.live / "shared" / "marker.txt").read_text() == "old\n"
        assert _git(self.live, "status", "--porcelain", "--untracked-files=no") == ""
        assert self.records() == records
        assert not (self.live / "deployed-service-images.compose.yml").exists()
        assert not self.secrets.exists()
        assert not any(
            line.startswith(("retag", "docker compose")) and " up " in line
            for line in self.host_log()
        )
        assert not self.stage.exists(), "the stage is removed however the step ends"
        assert _git(self.live, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.fixture
def deploy_host(tmp_path: Path) -> DeployHost:
    return DeployHost(tmp_path)


def test_the_verify_step_leaves_a_pending_set_and_nothing_live(deploy_host: DeployHost):
    before = deploy_host.records()

    result = deploy_host.verify()

    assert result.returncode == 0, result.stdout + result.stderr
    deploy_host.assert_nothing_live_changed(before)
    pending = deploy_host.pending
    assert sorted(path.name for path in pending.iterdir()) == [
        "deployed-service-images.compose.yml",
        "deployed-service-images.json",
        "deployed-worker-images.json",
        "release_switch.py",
    ]
    services = yaml.safe_load((pending / "deployed-service-images.compose.yml").read_text())
    assert services["services"]["api"] == {"image": REAL_RECORD["images"]["api"]["reference"]}
    assert json.loads((pending / "deployed-service-images.json").read_text())["git_sha"] == (
        deploy_host.new
    )
    compose = [line for line in deploy_host.host_log() if line.startswith("docker compose")]
    assert any("pull --ignore-buildable --policy missing --quiet" in line for line in compose)
    assert any(
        ".release-out/deployed-service-images.compose.yml config --quiet" in line
        for line in compose
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
def test_a_refusal_in_verify_leaves_no_pending_set_and_nothing_live(
    deploy_host: DeployHost, failure: dict[str, str]
):
    before = deploy_host.records()

    result = deploy_host.verify(**failure)

    assert result.returncode != 0
    deploy_host.assert_nothing_live_changed(before)
    assert not deploy_host.pending.exists()


def test_a_stale_pending_set_and_stage_are_discarded_by_the_next_verify(deploy_host: DeployHost):
    deploy_host.pending.mkdir()
    (deploy_host.pending / "deployed-worker-images.json").write_text('{"git_sha": "stale"}\n')
    (deploy_host.live / f"{PENDING}.next").mkdir()
    deploy_host.stage.mkdir()
    (deploy_host.stage / "leftover").write_text("from a run that died\n")

    refused = deploy_host.verify(FAKE_SERVICE_EXIT="9")

    assert refused.returncode != 0
    assert not deploy_host.pending.exists(), "a stale pending set never survives a verify"
    assert not (deploy_host.live / f"{PENDING}.next").exists()

    result = deploy_host.verify()

    assert result.returncode == 0, result.stdout + result.stderr
    worker = json.loads((deploy_host.pending / "deployed-worker-images.json").read_text())
    assert worker["git_sha"] == deploy_host.new


def test_a_revision_without_the_release_tooling_is_refused_on_the_host_before_any_pull(
    deploy_host: DeployHost,
):
    (deploy_host.live / "scripts" / "release_switch.py").unlink()
    _git(deploy_host.live, "commit", "-qam", "predates")
    predates = _git(deploy_host.live, "rev-parse", "HEAD")
    _git(deploy_host.live, "reset", "-q", "--hard", deploy_host.old)
    before = deploy_host.records()

    result = deploy_host.verify(revision=predates)

    assert result.returncode == 1
    assert "predates pulled service releases" in result.stderr
    assert not (deploy_host.root / "worker.started").exists()
    deploy_host.assert_nothing_live_changed(before)


def test_the_switch_applies_the_pending_set_in_order_and_promotes_after_up(
    deploy_host: DeployHost,
):
    assert deploy_host.verify().returncode == 0
    running = deploy_host.records()
    pending_worker = (deploy_host.pending / "deployed-worker-images.json").read_text()
    pending_service = (deploy_host.pending / "deployed-service-images.json").read_text()
    (deploy_host.root / "host.log").write_text("")

    result = deploy_host.switch()

    assert result.returncode == 0, result.stdout + result.stderr
    live = deploy_host.live
    assert (deploy_host.secrets / "github_app.pem").read_text() == "the-new-app-key\n"
    assert _git(live, "rev-parse", "HEAD") == deploy_host.new
    assert (live / "deployed-service-images.compose.yml").is_file()
    assert (live / "deployed-worker-images.json").read_text() == pending_worker
    assert (live / "deployed-service-images.json").read_text() == pending_service
    for chain in ("worker", "service"):
        assert (live / f"previous-deployed-{chain}-images.json").read_text() == (
            running[f"deployed-{chain}-images.json"]
        ), "the live current record, not the pending one, became previous"
    assert not deploy_host.pending.exists()
    log = deploy_host.host_log()
    # Run under `bash -s` like the real Switch, the script must reach its last command:
    # an ungrouped `compose exec` would eat the rest of it and still exit 0.
    assert any("--force-recreate" in line for line in log), "the Switch stopped early"
    retag = next(i for i, line in enumerate(log) if line.startswith("retag "))
    up = next(i for i, line in enumerate(log) if "up -d --remove-orphans" in line)
    schedulers = next(i for i, line in enumerate(log) if "--force-recreate" in line)
    assert log[retag] == f"retag {deploy_host.pending}/deployed-worker-images.json"
    assert retag < up < schedulers, log
    assert any("alembic upgrade head" in line for line in log[up:schedulers])
    assert any("seed_system_configs.py" in line for line in log[up:schedulers])


def test_an_incomplete_pending_set_is_refused_before_any_live_write(deploy_host: DeployHost):
    assert deploy_host.verify().returncode == 0
    (deploy_host.pending / "deployed-service-images.json").unlink()
    running = deploy_host.records()

    result = deploy_host.switch()

    assert result.returncode == 1
    assert "incomplete" in result.stderr
    deploy_host.assert_nothing_live_changed(running)


def test_a_pending_set_of_another_revision_is_refused_before_any_live_write(
    deploy_host: DeployHost,
):
    assert deploy_host.verify().returncode == 0
    running = deploy_host.records()

    result = deploy_host.switch(revision="c" * 40)

    assert result.returncode == 1
    assert "not of" in result.stderr
    deploy_host.assert_nothing_live_changed(running)


def test_a_failed_up_leaves_the_records_unpromoted(deploy_host: DeployHost):
    """Target partially applied: the live records still name what last came up."""
    assert deploy_host.verify().returncode == 0
    running = deploy_host.records()

    result = deploy_host.switch(FAKE_UP_EXIT="1")

    assert result.returncode != 0
    assert deploy_host.records() == running
    assert deploy_host.pending.is_dir(), "a rerun of the same revision verifies afresh"
    assert _git(deploy_host.live, "rev-parse", "HEAD") == deploy_host.new
