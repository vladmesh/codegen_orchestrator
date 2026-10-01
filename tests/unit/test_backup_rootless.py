"""The supported h01o user units and client select only their owning daemon."""

import configparser
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
from textwrap import dedent

import pytest
from test_backup_db import DOCKER

ROOT = Path(__file__).resolve().parents[2]
UNIT_DIR = ROOT / "infra/systemd"
CLIENT = ROOT / "infra/scripts/backup-db-rootless.sh"


def test_h01o_policy_and_user_unit_resolve_the_same_identity_and_files():
    policy = UNIT_DIR / "orchestrator-backup.env.example"
    result = subprocess.run(
        ["bash", "-c", 'set -a; . "$1"; env -0', "policy", str(policy)],
        env={},
        capture_output=True,
        check=True,
    )
    config = dict(item.split("=", 1) for item in result.stdout.decode().split("\0") if "=" in item)
    assert config["BACKUP_USER"] == "vlad"
    assert config["BACKUP_UID"] == "1001"
    assert config["BACKUP_RUNTIME_DIR"] == "/run/user/1001"
    assert config["COMPOSE_DIR"] == "/home/vlad/codegen_orchestrator"
    assert config["COMPOSE_ARGS"].split() == [
        "-p",
        "codegen_orchestrator",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.prod.yml",
        "-f",
        "/home/vlad/codegen-h01o.override.yml",
        "-f",
        "deployed-service-images.compose.yml",
    ]
    assert config["BACKUP_DIR"] == "/home/vlad/backups/orchestrator/nightly"
    assert config["BACKUP_KIND"] == "nightly"
    assert config["BACKUP_CONTOUR"] == "production"
    assert config["BACKUP_RETAIN"] == "7"
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(UNIT_DIR / "orchestrator-backup.service")
    assert "User" not in unit["Service"] and "Group" not in unit["Service"]
    assert "WorkingDirectory" not in unit["Service"]  # Compose root comes from policy.
    assert unit["Service"]["EnvironmentFile"] == "%h/.config/codegen-orchestrator/backup.env"
    assert unit["Service"]["ExecStart"] == "/usr/local/libexec/backup-db-rootless.sh backup"
    assert unit["Service"]["UMask"] == "0077"
    # These refer to the existing rootless daemon in the SAME user manager.
    assert unit["Unit"]["Requires"] == "docker.service"
    assert unit["Unit"]["After"] == "docker.service"
    assert "Install" not in unit
    timer = configparser.ConfigParser(interpolation=None)
    timer.read(UNIT_DIR / "orchestrator-backup.timer")
    assert timer["Timer"]["OnCalendar"] == "*-*-* 03:00:00 UTC"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_documented_user_installation_keeps_policy_and_backups_private(tmp_path):
    docs = (ROOT / "docs/DEPLOY.md").read_text()
    install = docs.split("# BEGIN user-owned backup installation\n")[1].split(
        "# END user-owned backup installation"
    )[0]
    account = tmp_path / "account"
    policy_dir = account / ".config/codegen-orchestrator"
    unit_dir = account / ".config/systemd/user"
    backups = account / "backups/orchestrator/nightly"
    result = subprocess.run(
        ["bash", "-eu", "-c", install],
        env={
            **os.environ,
            "BACKUP_RELEASE_ROOT": str(ROOT),
            "backup_policy_dir": str(policy_dir),
            "backup_policy": str(policy_dir / "backup.env"),
            "backup_unit_dir": str(unit_dir),
            "BACKUP_DIR": str(backups),
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    policy = policy_dir / "backup.env"
    assert policy.stat().st_mode & 0o777 == 0o600
    assert policy.stat().st_uid == os.getuid()
    for directory in [policy_dir, unit_dir, backups]:
        assert directory.stat().st_mode & 0o777 == 0o700
        assert directory.stat().st_uid == os.getuid()
    for name in ["orchestrator-backup.service", "orchestrator-backup.timer"]:
        assert (unit_dir / name).read_bytes() == (UNIT_DIR / name).read_bytes()
    policy.write_text("existing reviewed host policy\n")
    # Updating installed units must preserve the explicit host-specific policy.
    subprocess.run(
        ["bash", "-eu", "-c", install],
        env={
            **os.environ,
            "BACKUP_RELEASE_ROOT": str(ROOT),
            "backup_policy_dir": str(policy_dir),
            "backup_policy": str(policy),
            "backup_unit_dir": str(unit_dir),
            "BACKUP_DIR": str(backups),
        },
        check=True,
        capture_output=True,
        timeout=10,
    )
    assert policy.read_text() == "existing reviewed host policy\n"


def test_maintenance_configuration_and_fences_parse_as_bash():
    docs = (ROOT / "docs/runbooks/po-redis-and-checkpoints.md").read_text()
    blocks = re.findall(r"(?m)^[ \t]*```bash[^\n]*\n([\s\S]*?)^[ \t]*```[ \t]*$", docs)
    affected = [block for block in blocks if "BACKUP_DOCKER" in block]
    assert len(affected) == 6
    for block in affected:
        result = subprocess.run(
            ["bash", "-n"], input=dedent(block), text=True, capture_output=True, timeout=5
        )
        assert result.returncode == 0, result.stderr


@pytest.fixture
def rootless_client(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    commands = tmp_path / "commands.jsonl"
    # A foreign default context would select the colocated system daemon. The
    # fixture rejects it unless either --host or the cleared-context env binds
    # the explicitly configured socket. Payload behavior is the accepted helper fixture.
    docker = DOCKER.replace(
        "args = sys.argv[1:]",
        """args = sys.argv[1:]
endpoint = "unix://" + os.environ["BACKUP_RUNTIME_DIR"] + "/docker.sock"
with open(os.environ["ENDPOINT_LOG"], "a") as log:
    log.write(json.dumps({"args": args, "host": os.environ.get("DOCKER_HOST"),
                          "context": os.environ.get("DOCKER_CONTEXT")}) + "\\n")
if args[:2] == ["--host", endpoint]:
    args = args[2:]
else:
    assert not os.environ.get("DOCKER_CONTEXT"), "foreign context selected"
    assert os.environ.get("DOCKER_HOST") == endpoint, "foreign daemon selected"
""",
    )
    (binaries / "docker").write_text(docker)
    (binaries / "docker").chmod(0o755)
    for command in ["systemctl", "journalctl"]:
        (binaries / command).write_text('#!/bin/sh\ntest "$1" = --user\n')
        (binaries / command).chmod(0o755)
    installed = tmp_path / "libexec"
    installed.mkdir()
    # Same filenames as the installed operation, independent of the checkout.
    shutil.copyfile(CLIENT, installed / CLIENT.name)
    (installed / CLIENT.name).chmod(0o755)
    shutil.copyfile(ROOT / "infra/scripts/backup-db.sh", installed / "backup-db.sh")
    (installed / "backup-db.sh").chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    compose = tmp_path / "compose"
    compose.mkdir()
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "BACKUP_USER": subprocess.check_output(["id", "-un"], text=True).strip(),
        "BACKUP_UID": str(os.getuid()),
        "BACKUP_RUNTIME_DIR": str(runtime),
        "COMPOSE_DIR": str(compose),
        "COMPOSE_ARGS": "-f docker-compose.yml -f host.yml",
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_KIND": "nightly",
        "BACKUP_CONTOUR": "production",
        "BACKUP_RETAIN": "2",
        "COMMAND_LOG": str(commands),
        "ENDPOINT_LOG": str(tmp_path / "endpoints.jsonl"),
        "DOCKER_CONTEXT": "foreign-system-daemon",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_TLS_VERIFY": "1",
        "DOCKER_CERT_PATH": "/foreign/certificates",
    }
    with socket.socket(socket.AF_UNIX) as daemon:
        daemon.bind(str(runtime / "docker.sock"))
        yield installed / CLIENT.name, env


def invoke(client, env, *args):
    return subprocess.run(["bash", str(client), *args], env=env, capture_output=True, timeout=15)


def test_backup_and_independent_readback_ignore_foreign_operator_context(rootless_client):
    client, env = rootless_client
    result = invoke(client, env, "backup")
    assert result.returncode == 0, result.stderr.decode()
    archive = next(Path(env["BACKUP_DIR"]).glob("*.dump"))
    assert archive.stat().st_mode & 0o777 == 0o600
    assert archive.parent.stat().st_mode & 0o777 == 0o700
    policy = client.parent.parent / "backup.env"
    policy.write_text(
        "\n".join(
            f"{key}={shlex.quote(value)}"
            for key, value in env.items()
            if key.startswith(("BACKUP_", "COMPOSE_"))
        )
    )
    policy.chmod(0o600)
    docs = (ROOT / "docs/DEPLOY.md").read_text()
    readback_script = (
        docs.split("# BEGIN owning-user archive readback\n")[1]
        .split("# END owning-user archive readback")[0]
        .replace("/usr/local/libexec", str(client.parent))
    )
    # Execute the actual documented readback. Only installed paths and native
    # service-manager inventory are replaced; the client checks identity/permissions
    # and the controlled Docker fixture checks endpoint selection.
    readback = subprocess.run(
        ["bash", "-eu", "-c", readback_script],
        env={**env, "backup_policy": str(policy), "VERIFIED_BACKUP_PATH": str(archive)},
        capture_output=True,
        timeout=15,
    )
    assert readback.returncode == 0, readback.stderr.decode()
    assert b"archive_list_exit=0" in readback.stdout
    assert b"synthetic-dump-secret-canary" not in readback.stdout + readback.stderr
    records = [json.loads(line) for line in Path(env["ENDPOINT_LOG"]).read_text().splitlines()]
    endpoint = f"unix://{env['BACKUP_RUNTIME_DIR']}/docker.sock"
    assert len(records) == 6
    assert all(record["host"] == endpoint and record["context"] is None for record in records)
    assert all(record["args"][:2] == ["--host", endpoint] for record in records[-2:])


@pytest.mark.parametrize(
    "failure", ["identity", "user", "runtime", "runtime_mode", "socket", "symlink", "regular"]
)
@pytest.mark.parametrize("mode", ["backup", "docker"])
def test_absent_or_mismatched_rootless_precondition_never_calls_docker(
    rootless_client, failure, mode
):
    client, env = rootless_client
    runtime = Path(env["BACKUP_RUNTIME_DIR"])
    if failure == "identity":
        env["BACKUP_UID"] = str(os.getuid() + 1)
    elif failure == "user":
        env["BACKUP_USER"] = "another-owner"
    elif failure == "runtime":
        env["BACKUP_RUNTIME_DIR"] = str(runtime / "absent")
    elif failure == "runtime_mode":
        runtime.chmod(0o755)
    elif failure == "socket":
        (runtime / "docker.sock").unlink()
    elif failure == "symlink":
        (runtime / "docker.sock").rename(runtime / "another.sock")
        (runtime / "docker.sock").symlink_to(runtime / "another.sock")
    else:
        (runtime / "docker.sock").unlink()
        (runtime / "docker.sock").write_text("not a socket")
    result = invoke(client, env, mode, "compose", "version")
    assert result.returncode != 0
    assert b"[backup-rootless] failed operation=" in result.stderr
    assert not Path(env["COMMAND_LOG"]).exists()
    assert not Path(env["BACKUP_DIR"]).exists()


@pytest.mark.parametrize("name", ["BACKUP_USER", "BACKUP_UID", "BACKUP_RUNTIME_DIR"])
def test_rootless_configuration_is_required(rootless_client, name):
    client, env = rootless_client
    env.pop(name)
    result = invoke(client, env, "backup")
    assert result.returncode != 0
    assert name.encode() in result.stderr
    assert not Path(env["COMMAND_LOG"]).exists()
