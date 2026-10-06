"""Execute the stand workflow's no-model path with synthetic command boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts import stand_acceptance, stand_preflight, stand_run

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github/workflows/stand-e2e.yml"
RAW_TARGETS = ("tests/live/test_llm_channel_failover.py", "tests/live/test_unknown_target.py")


def steps():
    return {s["name"]: s for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e"]["steps"]}


def run(script, tmp_path, **env):
    return subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), **env},
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.fixture
def commands(tmp_path):
    """External commands are synthetic; profile/files/output handling stays real."""
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "command"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import hashlib, json, os, pathlib, shutil, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "if name == 'uuidgen': print('synthetic-redaction-canary')\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
        "    log.write(json.dumps([name, *args]) + '\\n')\n"
        "if name == 'docker':\n"
        "    if args[-1] == '--version':\n"
        "        print(os.environ.get('SYNTHETIC_VERSION', 'codex-cli 0.144.6'))\n"
        "    elif args[-1] in ('-u', '-g'): print('1000')\n"
        "    elif 'exec' in args:\n"
        "        mount = args[args.index('--mount') + 1]\n"
        "        source = mount.split('source=', 1)[1].split(',', 1)[0]\n"
        "        profile = pathlib.Path(source) / 'auth.json'\n"
        "        profile.write_text(json.dumps({'tokens': {\n"
        "            'access_token': 'synthetic-rotated-access',\n"
        "            'refresh_token': 'synthetic-rotated-refresh'}}))\n"
        "    else: sys.exit(90)\n"
        "if name in ('ssh', 'scp') and os.environ.get('UNREACHABLE'):\n"
        "    print('synthetic transfer refusal: host unreachable', file=sys.stderr)\n"
        "    sys.exit(255)\n"
        "if name == 'gh':\n"
        "    digest = hashlib.sha256(sys.stdin.buffer.read()).hexdigest()\n"
        "    receipt = pathlib.Path(os.environ['RUNNER_TEMP']) / 'persisted-profile.sha256'\n"
        "    receipt.write_text(digest)\n"
        "if name == 'scp' and os.environ.get('REMOTE_AUTH_SOURCE'):\n"
        "    shutil.copyfile(os.environ['REMOTE_AUTH_SOURCE'], args[-1])\n"
    )
    script.chmod(0o755)
    for name in ("docker", "sudo", "ssh", "scp", "gh", "uuidgen", "uv"):
        (directory / name).symlink_to(script)
    return {
        "PATH": f"{directory}:{os.environ['PATH']}",
        "COMMAND_LOG": str(tmp_path / "commands.jsonl"),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }


def execute_step(name, tmp_path, commands, llm, **env):
    step = steps()[name]
    if not enabled(step, llm):
        return None
    return run(step["run"], tmp_path, **{**commands, "MODEL_SESSIONS": llm, **env})


def logged(commands):
    path = Path(commands["COMMAND_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def workflow_selection(tmp_path, requested, target=""):
    output = tmp_path / "selection-output"
    script = (
        steps()["Resolve the suite"]["run"]
        .replace("${{ inputs.suite }}", requested)
        .replace("${{ inputs.target }}", target)
    )
    result = run(script, tmp_path, GITHUB_OUTPUT=str(output), TEMPLATE_SOURCE="", TEMPLATE_REF="")
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def cleanup_selection(selection):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    outputs = {
        name: selection[expression.removeprefix("${{ steps.suite.outputs.").removesuffix(" }}")]
        for name, expression in workflow["jobs"]["e2e"]["outputs"].items()
    }
    final = next(
        s for s in workflow["jobs"]["cleanup"]["steps"] if s["name"] == "Admit final artifact"
    )
    return {
        name: outputs.get(
            final["env"][name].removeprefix("${{ needs.e2e.outputs.").removesuffix(" }}"), ""
        )
        for name in ("SUITE", "MODEL_SESSIONS")
    }


@pytest.mark.parametrize("target", RAW_TARGETS)
def test_raw_custom_targets_restore_authenticate_and_persist_before_provisioning(
    tmp_path, commands, target
):
    selection = workflow_selection(tmp_path, "custom", target)
    assert selection["model_sessions"] == "true"
    assert not stand_run.resolve_suite(target)[1].llm
    assert (
        stand_run.suite_environment(stand_run.resolve_suite(target)[1], qa="codex", worker="claude")
        == {}
    )
    for name in (
        "Restore refreshable Codex auth profile",
        "Verify exact worker image CLI and identity",
        "Authenticate Codex against exact worker image",
        "Persist preflight-refreshed Codex auth profile",
    ):
        result = execute_step(
            name,
            tmp_path,
            commands,
            selection["model_sessions"],
            CODEX_AUTH_JSON=json.dumps(
                {
                    "tokens": {
                        "access_token": "synthetic-access",
                        "refresh_token": "synthetic-refresh",
                    }
                }
            ),
            CODEX_WORKER_UID="1000",
            CODEX_WORKER_GID="1000",
            GH_TOKEN="synthetic-write-token",  # noqa: S106 - synthetic external command
            GITHUB_REPOSITORY="example/repo",
        )
        assert result is not None
        assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "stand-codex-profile/auth.json").is_file()
    assert any("exec" in call for call in logged(commands))
    assert any(call[:3] == ["gh", "secret", "set"] for call in logged(commands))
    renderer = steps()["Render protected dynamic configuration"]
    configuration = dict.fromkeys(renderer["env"], "synthetic-value")
    configuration.update(
        MODEL_SESSIONS=selection["model_sessions"],
        QA_TELETHON=selection["qa_telethon"],
        STAND_CLAUDE_CODE_OAUTH_TOKEN="synthetic-retained-claude-session",  # noqa: S106
    )
    result = run(renderer["run"], tmp_path, **configuration)
    assert result.returncode == 0, result.stderr
    assert (
        "STAND_CLAUDE_CODE_OAUTH_TOKEN=synthetic-retained-claude-session\n"
        in (tmp_path / ".stand.env").read_text()
    )
    (tmp_path / "stand-bootstrap-started").write_text("0\n")
    result = execute_step(
        "Bring up dynamic orchestrator and wait for API",
        tmp_path,
        commands,
        selection["model_sessions"],
        PROD_HOST="192.0.2.1",
        RUNTIME_UID="1000",
        RUNTIME_GID="1000",
        CODEX_WORKER_UID="1000",
        CODEX_WORKER_GID="1000",
        SSH_OPTS="",
        STAND_BACKGROUND_DIR=str(tmp_path / "background"),
        STAND_SERVICE_RELEASE_COMPOSE="release.yml",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stand-codex-remote-profile-installed").exists()
    assert any(str(tmp_path / "stand-codex-profile/auth.json") in call for call in logged(commands))


@pytest.mark.parametrize("target", RAW_TARGETS)
def test_raw_custom_precreate_credentials_refuse_before_provider_boundary(
    tmp_path, commands, target
):
    # Route only the actual provider module to a synthetic executable. All credential
    # validators, shell order and file boundaries are still the shipped workflow.
    import shlex

    wrapper = tmp_path / "bin/python3"
    provider = tmp_path / "bin/provider-boundary"
    provider.symlink_to(tmp_path / "bin/command")
    wrapper.write_text(
        '#!/bin/bash\nif [ "${2-}" = scripts.stand_lifecycle ]; then\n'
        f'  exec {shlex.quote(sys.executable)} {shlex.quote(str(provider))} "$@"\n'
        f'fi\nexec {shlex.quote(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o755)
    script = "\n".join(
        steps()[name]["run"]
        for name in (
            "Validate pre-create credentials",
            "Preflight ephemeral machines",
            "Create ephemeral machines",
        )
    )
    key = "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----"
    result = run(
        script,
        tmp_path,
        **commands,
        SUITE=target,
        SSH_PRIVATE_KEY=key,
        STAND_RUN_TAG="gha-synthetic",
    )
    assert result.returncode != 0
    assert "Claude token" in result.stdout
    assert "Telethon session" in result.stdout
    assert not any(call[0] == "provider-boundary" for call in logged(commands))


@pytest.mark.parametrize(
    ("suite", "target", "expected_sessions"),
    [
        ("mega-noop", "", "false"),
        ("mega-live", "", "true"),
        ("mega-brief", "", "true"),
        ("mega-brief-package", "", "true"),
        ("mega", "", "false"),
        ("custom", "tests/live/test_api_crud.py", "true"),
        ("custom", "mega-live", "true"),
        ("custom", "mega-brief", "true"),
        ("custom", "mega-brief-package", "true"),
        ("custom", "mega-noop", "false"),
        ("custom", "mega", "false"),
    ],
)
def test_resolved_workflow_uses_canonical_session_requirement(
    tmp_path, suite, target, expected_sessions
):
    from scripts.stand_run import resolve_suite

    output = tmp_path / "output"
    script = (
        steps()["Resolve the suite"]["run"]
        .replace("${{ inputs.suite }}", suite)
        .replace("${{ inputs.target }}", target)
    )
    result = run(script, tmp_path, GITHUB_OUTPUT=str(output), TEMPLATE_SOURCE="", TEMPLATE_REF="")
    assert result.returncode == 0, result.stderr
    resolved = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert resolved["value"] == resolve_suite(target if suite == "custom" else suite)[0]
    assert resolved["model_sessions"] == expected_sessions


def enabled(step, llm):
    condition = step.get("if", "success()")
    condition = condition.removeprefix("${{ ").removesuffix(" }}")
    condition = condition.replace("steps.suite.outputs.model_sessions", repr(llm))
    condition = condition.replace(
        "steps.codex-auth-preflight.outcome", repr("success" if llm == "true" else "skipped")
    )
    condition = (
        condition.replace("success()", "True").replace("always()", "True").replace("&&", "and")
    )
    return eval(condition, {"__builtins__": {}})  # noqa: S307 - controlled workflow conditions


@pytest.mark.parametrize("llm", ["false", "true"])
def test_noop_skips_profile_authentication_and_rotation_while_live_requires_them(llm):
    for name in (
        "Restore refreshable Codex auth profile",
        "Authenticate Codex against exact worker image",
        "Persist preflight-refreshed Codex auth profile",
        "Persist refreshed Codex auth profile",
    ):
        assert enabled(steps()[name], llm) is (llm == "true"), name


def test_noop_runtime_preflight_needs_no_model_session(monkeypatch):
    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setattr(sys, "argv", ["stand_preflight", "--suite", "mega-noop"])
    for name in ("check_contour", "check_docker", "check_disk", "check_deploy_target"):
        monkeypatch.setattr(stand_preflight, name, lambda *args: ("synthetic", True, ""))

    def forbidden(*args):
        pytest.fail("no-model suite tried to validate a model session")

    monkeypatch.setattr(stand_preflight, "check_stand_token_credentials", forbidden)
    monkeypatch.setattr(stand_preflight, "check_codex_session", forbidden)
    assert stand_preflight.main() == 0


@pytest.mark.parametrize("llm", ["false", "true"])
def test_image_identity_and_auth_selection_execute_real_workflow(tmp_path, commands, llm):
    for name in (
        "Restore refreshable Codex auth profile",
        "Verify exact worker image CLI and identity",
        "Authenticate Codex against exact worker image",
        "Persist preflight-refreshed Codex auth profile",
        "Persist refreshed Codex auth profile",
    ):
        result = execute_step(
            name,
            tmp_path,
            commands,
            llm,
            CODEX_AUTH_JSON=json.dumps(
                {
                    "tokens": {
                        "access_token": "synthetic-access",
                        "refresh_token": "synthetic-refresh",
                    }
                }
            )
            if llm == "true"
            else "",
            GH_TOKEN="synthetic-write-token" if llm == "true" else "",
            GITHUB_REPOSITORY="example/repo",
            CODEX_WORKER_UID="1000",
            CODEX_WORKER_GID="1000",
        )
        if result is not None:
            assert result.returncode == 0, result.stderr
    calls = logged(commands)
    assert any(call[-1] == "--version" for call in calls)
    assert any(call[-1] == "-u" for call in calls)
    assert any(call[-1] == "-g" for call in calls)
    assert sum("exec" in call for call in calls) == (llm == "true")
    assert sum(call[:3] == ["gh", "secret", "set"] for call in calls) == (llm == "true")
    assert (tmp_path / "stand-codex-profile/auth.json").exists() is (llm == "true")
    assert "worker_uid=1000" in (tmp_path / "output").read_text()
    if llm == "true":
        import hashlib

        profile = tmp_path / "stand-codex-profile/auth.json"
        assert "synthetic-rotated-refresh" in profile.read_text()
        assert (tmp_path / "persisted-profile.sha256").read_text() == hashlib.sha256(
            profile.read_bytes()
        ).hexdigest()


def test_noop_precreate_without_sessions_still_requires_ssh(tmp_path):
    script = steps()["Validate pre-create credentials"]["run"]
    key = "-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----"
    result = run(script, tmp_path, SUITE="mega-noop", SSH_PRIVATE_KEY=key)
    assert result.returncode == 0, result.stderr
    result = run(script, tmp_path, SUITE="mega-noop")
    assert result.returncode != 0
    assert "SSH material" in result.stdout
    result = run(script, tmp_path, SUITE="mega-live", SSH_PRIVATE_KEY=key)
    assert result.returncode != 0
    assert "Claude token" in result.stdout
    assert "Telethon session" in result.stdout


def test_noop_host_install_has_no_profile_or_auth_output_dependency(tmp_path, commands):
    (tmp_path / "stand-bootstrap-started").write_text("0\n")
    for name in (".stand.env", ".stand-qa-worker.env", ".stand-github-app.pem"):
        (tmp_path / name).write_text("synthetic")
    result = execute_step(
        "Bring up dynamic orchestrator and wait for API",
        tmp_path,
        commands,
        "false",
        PROD_HOST="192.0.2.1",
        RUNTIME_UID="1000",
        RUNTIME_GID="1000",
        CODEX_WORKER_UID="",
        CODEX_WORKER_GID="",
        SSH_OPTS="",
        STAND_BACKGROUND_DIR=str(tmp_path / "background"),
        STAND_SERVICE_RELEASE_COMPOSE="release.yml",
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "stand-codex-remote-profile-installed").exists()
    assert not any("stand-codex" in " ".join(call) for call in logged(commands))


def test_unreachable_suite_preserves_identity_failure_and_runner_evidence(tmp_path, commands):
    env = {
        **commands,
        "PROD_HOST": "192.0.2.1",
        "SUITE": "mega-noop",
        "WORKER": "claude",
        "QA": "codex",
        "TEMPLATE_SOURCE": "",
        "TEMPLATE_REF": "",
        "STAND_PRODUCT_BOT_TOKEN": "synthetic-bot-secret",
        "SSH_OPTS": "",
        "STAND_BACKGROUND_DIR": str(tmp_path / "background"),
        "STAND_SERVICE_RELEASE_COMPOSE": "release.yml",
        "GITHUB_WORKFLOW": "stand-e2e",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "a" * 40,
        "STAND_RUN_TAG": "gha-123-2",
        "UNREACHABLE": "true",
    }
    (tmp_path / "machines.json").write_text(
        '{"run_tag":"gha-123-2","orchestrator":{"id":"synthetic-machine"}}'
    )
    result = run(steps()["Run selected stand suite"]["run"], tmp_path, **env)
    assert result.returncode == 255
    primary = (tmp_path / "remote-invocation.log").read_text()
    for fact in (
        "mega-noop",
        "gha-123-2",
        "123",
        "192.0.2.1",
        "exit=255",
        "synthetic transfer refusal",
    ):
        assert fact in primary
    assert "synthetic-bot-secret" not in primary
    result = run(
        steps()["Record machine manifest"]["run"], tmp_path, **{**env, "SUITE_OUTCOME": "failure"}
    )
    assert result.returncode == 0, result.stderr
    handoff = tmp_path / "stand-e2e-handoff"
    assert (handoff / "machines.json").is_file()
    assert (handoff / "run/remote-invocation.log").read_text().startswith(primary)
    assert "unavailable" in (handoff / "run/suite-services.log").read_text()
    assert not (handoff / "run/junit.xml").exists()
    cleanup = tmp_path / "cleanup.json"
    cleanup.write_text(
        json.dumps(
            {
                "run_tag": "gha-123-2",
                "status": "verified",
                "observed_at": "2026-10-04T00:00:00Z",
                "remaining_ids": [],
                "errors": [],
            }
        )
    )
    output = tmp_path / "acceptance"
    complete = stand_acceptance.build_acceptance_artifact(
        manifest_path=handoff / "machines.json",
        run_dir=handoff / "run",
        cleanup_path=cleanup,
        output=output,
    )
    assert not complete
    report = json.loads((output / "final-report.json").read_text())
    assert "required_run_output_missing:junit.xml" in report["incompleteness"]


@pytest.mark.parametrize("suite", ["mega-noop", "mega"])
def test_noop_handoff_and_final_admission_without_profile_attestation(tmp_path, commands, suite):
    protected = {
        name: f"synthetic-{name.lower()}-value"
        for name in stand_acceptance.PROTECTED_STAND_SECRET_NAMES
    }
    for name in ("STAND_CLAUDE_CODE_OAUTH_TOKEN", "TELETHON_API_HASH", "TELETHON_SESSION"):
        protected.pop(name)
    handoff = tmp_path / "stand-e2e-handoff"
    handoff.mkdir()
    (handoff / "run").mkdir()
    (handoff / "run/remote-invocation.log").write_text("synthetic transfer refusal exit=255\n")
    result = execute_step(
        "Admit cleanup handoff", tmp_path, commands, "false", SUITE=suite, **protected
    )
    assert result.returncode == 0, result.stderr + result.stdout

    assert not (handoff / "profile-redaction-attestation.json").exists()
    final = tmp_path / "stand-acceptance"
    final.mkdir()
    (final / "remote-invocation.log").write_text("synthetic transfer refusal exit=255\n")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    script = next(
        s for s in workflow["jobs"]["cleanup"]["steps"] if s["name"] == "Admit final artifact"
    )["run"]
    result = run(
        script,
        tmp_path,
        **{**commands, **protected, **cleanup_selection(workflow_selection(tmp_path, suite))},
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("suite", ["mega-live", *RAW_TARGETS, None, ""])
def test_paid_admission_refuses_skipped_profile_redaction(tmp_path, suite):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    env = {
        name: f"synthetic-{name.lower()}-value"
        for name in stand_acceptance.PROTECTED_STAND_SECRET_NAMES
    }
    import shlex

    selection = "" if suite is None else " --suite " + shlex.quote(suite)
    result = run(
        "python3 -m scripts.stand_acceptance admit --artifact artifact "
        "--status status.json --protected-env" + selection,
        tmp_path,
        **env,
    )
    assert result.returncode == 2
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "rejected"


def test_profile_attestation_requires_profile_needles(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    env = {
        name: f"synthetic-{name.lower()}-value"
        for name in stand_acceptance.PROTECTED_STAND_SECRET_NAMES
    }
    result = run(
        "python3 -m scripts.stand_acceptance admit --artifact artifact "
        "--status status.json --protected-env --profile-attestation marker.json",
        tmp_path,
        **env,
    )
    assert result.returncode != 0
    assert not (tmp_path / "marker.json").exists()


@pytest.mark.parametrize("suite", ["mega-live", *RAW_TARGETS, None, ""])
def test_paid_runtime_preflight_missing_sessions_refuses_value_free(
    monkeypatch, tmp_path, capsys, suite
):
    monkeypatch.setenv("LIVE_CONTOUR", "stand")
    monkeypatch.setenv("HOST_CODEX_HOME", str(tmp_path / "absent-profile"))
    for name in ("STAND_CLAUDE_CODE_OAUTH_TOKEN", "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT"):
        monkeypatch.delenv(name, raising=False)
    for name in ("check_contour", "check_docker", "check_disk", "check_deploy_target"):
        monkeypatch.setattr(stand_preflight, name, lambda *args: ("synthetic", True, ""))
    assert stand_preflight.main([] if suite is None else ["--suite", suite]) == 1
    output = capsys.readouterr().out
    assert "Claude token: is missing" in output
    assert "FAIL codex session" in output


def test_noop_runner_passes_classification_to_preflight_and_strips_inherited_agents(
    monkeypatch, tmp_path, commands
):
    monkeypatch.setenv("LIVE_LLM_QA", "1")
    monkeypatch.setenv("LIVE_QA_AGENT_TYPE", "codex")
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", "claude")
    captured = tmp_path / "pytest-env.json"
    (tmp_path / "bin/uv").unlink()
    uv = tmp_path / "bin/uv"
    uv.write_text(
        "#!/usr/bin/env python3\nimport json, os\n"
        f"with open({str(captured)!r}, 'w') as output:\n"
        "    json.dump({name: os.environ[name] for name in "
        f"{stand_run.LLM_ENV_NAMES!r} if name in os.environ}}, output)\n"
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", commands["PATH"])
    monkeypatch.setattr(stand_run, "read_env_file", lambda path: {})
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "entry_refusal", lambda *args, **kwargs: None)
    monkeypatch.setattr(stand_run, "sweep", lambda *args: True)
    seen = []

    def preflight(env, log):
        seen.append(env["STAND_SUITE"])
        # Run the actual preflight entry with only non-model infrastructure synthetic.
        for name in ("check_contour", "check_docker", "check_disk", "check_deploy_target"):
            monkeypatch.setattr(stand_preflight, name, lambda *args: ("synthetic", True, ""))
        monkeypatch.setenv("LIVE_CONTOUR", "stand")
        return stand_preflight.main(["--suite", env["STAND_SUITE"]]) == 0

    monkeypatch.setattr(stand_run, "preflight", preflight)
    monkeypatch.setattr(
        sys, "argv", ["stand_run", "--suite", "mega-noop", "--qa", "codex", "--worker", "claude"]
    )
    assert stand_run.main() == 0
    assert seen == ["mega-noop"]
    child_env = json.loads(captured.read_text())
    assert not set(stand_run.LLM_ENV_NAMES) & child_env.keys()


def test_noop_renderer_requires_service_credentials_but_no_model_sessions(tmp_path):
    step = steps()["Render protected dynamic configuration"]
    env = dict.fromkeys(step["env"], "synthetic-value")
    for name in (
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "TELETHON_API_ID",
        "TELETHON_API_HASH",
        "TELETHON_SESSION",
    ):
        env.pop(name)
    env.update(MODEL_SESSIONS="false", QA_TELETHON="false")
    result = run(step["run"], tmp_path, **env)
    assert result.returncode == 0, result.stderr
    assert "STAND_CLAUDE_CODE_OAUTH_TOKEN=\n" in (tmp_path / ".stand.env").read_text()
    env.pop("GH_APP_PRIVATE_KEY")
    result = run(step["run"], tmp_path, **env)
    assert result.returncode != 0
    assert "GH_APP_PRIVATE_KEY" in result.stderr


@pytest.mark.parametrize("llm", ["false", "true"])
def test_cli_version_mismatch_refuses_before_authentication(tmp_path, commands, llm):
    result = execute_step(
        "Verify exact worker image CLI and identity",
        tmp_path,
        commands,
        llm,
        SYNTHETIC_VERSION="codex-cli 0.0.0",
    )
    assert result.returncode != 0
    assert "codex_cli_mismatch" in result.stderr
    assert not any("exec" in call for call in logged(commands))


def test_paid_restore_missing_session_refuses_without_secret_values(tmp_path, commands):
    result = execute_step(
        "Restore refreshable Codex auth profile", tmp_path, commands, "true", CODEX_AUTH_JSON=""
    )
    assert result.returncode != 0
    assert "codex_auth_missing" in result.stderr
    assert not (tmp_path / "stand-codex-profile/auth.json").exists()


@pytest.mark.parametrize("suite", ["mega-live", *RAW_TARGETS])
def test_paid_handoff_profile_needles_and_final_attestation_are_required(tmp_path, commands, suite):
    protected = {
        name: f"synthetic-{name.lower()}-value"
        for name in stand_acceptance.PROTECTED_STAND_SECRET_NAMES
    }
    handoff = tmp_path / "stand-e2e-handoff"
    (handoff / "run").mkdir(parents=True)
    for name in ("stand-codex-initial-auth.json", "stand-codex-profile/auth.json"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": "synthetic-access-needle",
                        "refresh_token": "synthetic-refresh-needle",
                    }
                }
            )
        )
    artifact = handoff / "run/remote-invocation.log"
    artifact.write_text("synthetic-access-needle")
    result = execute_step(
        "Admit cleanup handoff", tmp_path, commands, "true", SUITE=suite, **protected
    )
    assert result.returncode == 2
    marker = handoff / "profile-redaction-attestation.json"
    assert not marker.exists()
    artifact.write_text("synthetic value-free ssh failure")
    result = execute_step(
        "Admit cleanup handoff", tmp_path, commands, "true", SUITE=suite, **protected
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(marker.read_text()) == {"marker": stand_acceptance.PROFILE_ATTESTATION_MARKER}
    final = tmp_path / "stand-acceptance"
    final.mkdir()
    (final / "remote-invocation.log").write_text("synthetic value-free ssh failure")
    script = next(
        s
        for s in yaml.safe_load(WORKFLOW.read_text())["jobs"]["cleanup"]["steps"]
        if s["name"] == "Admit final artifact"
    )["run"]
    selection = workflow_selection(tmp_path, "custom", suite)
    env = {**commands, **protected, **cleanup_selection(selection)}
    result = run(script, tmp_path, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    marker.unlink()
    result = run(script, tmp_path, **env)
    assert result.returncode == 2


def test_noop_still_scans_unused_session_values_if_supplied(tmp_path, commands):
    handoff = tmp_path / "stand-e2e-handoff"
    (handoff / "run").mkdir(parents=True)
    protected = {
        name: f"synthetic-{name.lower()}-value"
        for name in stand_acceptance.PROTECTED_STAND_SECRET_NAMES
    }
    (handoff / "run/remote-invocation.log").write_text(protected["STAND_CLAUDE_CODE_OAUTH_TOKEN"])
    result = execute_step(
        "Admit cleanup handoff", tmp_path, commands, "false", SUITE="mega-noop", **protected
    )
    assert result.returncode == 2


def test_paid_remote_rotation_requires_refreshable_profile_and_persists_it(tmp_path, commands):
    import hashlib

    (tmp_path / "stand-codex-remote-profile-installed").touch()
    (tmp_path / "stand-bootstrap.key").touch()
    remote = tmp_path / "remote-profile.json"
    remote.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "synthetic-remote-access",
                    "refresh_token": "synthetic-remote-refresh",
                }
            }
        )
    )
    env = {
        "PROD_HOST": "192.0.2.1",
        "SSH_OPTS": "",
        "GH_TOKEN": "synthetic-write-token",
        "GITHUB_REPOSITORY": "example/repo",
        "REMOTE_AUTH_SOURCE": str(remote),
    }
    result = execute_step("Persist refreshed Codex auth profile", tmp_path, commands, "true", **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "stand-codex-refreshed-auth-valid").exists()
    assert (tmp_path / "persisted-profile.sha256").read_text() == hashlib.sha256(
        remote.read_bytes()
    ).hexdigest()
    before = logged(commands)
    remote.write_text('{"tokens":{"access_token":"synthetic-only-access"}}')
    result = execute_step("Persist refreshed Codex auth profile", tmp_path, commands, "true", **env)
    assert result.returncode != 0
    assert "not refresh-capable" in result.stderr
    assert sum(call[0] == "gh" for call in logged(commands)) == sum(
        call[0] == "gh" for call in before
    )


def test_suite_resolver_imports_without_installed_dependencies():
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "from scripts.stand_run import resolve_suite; "
            "assert not resolve_suite('mega-noop')[1].llm; "
            "assert resolve_suite('mega-live')[1].llm",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("suite", ["mega-noop", "mega-live", "mega-brief-package"])
def test_runner_preflight_forwards_the_canonical_suite(monkeypatch, suite):
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="synthetic infrastructure checks")

    monkeypatch.setattr(stand_run.subprocess, "run", command)
    assert stand_run.preflight({"STAND_SUITE": suite}, lambda line: None)
    assert calls[0][0][-2:] == ["--suite", suite]
    assert calls[0][1]["LIVE_CONTOUR"] == "stand"
