"""Run the backup boundary with deterministic Docker commands, without a daemon."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra/scripts/backup-db.sh"
CANARY = "synthetic-dump-secret-canary"
DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
failure = os.environ.get("FAILURE", "")
if args[0] == "compose":
    assert "-C" not in args
    assert args[1:3] == ["--project-directory", os.environ["COMPOSE_DIR"]]
    assert args[3:7] == ["-f", "docker-compose.yml", "-f", "host.yml"]
    assert args[7:] == ["ps", "--all", "-q", "db"]
    if failure == "connect":
        sys.exit(1)
    if failure != "missing":
        print("a" * 64)
elif args[0] == "inspect":
    print("false" if failure == "stopped" else "true")
elif args[0] == "exec":
    if "pg_dump" in args[-1]:
        assert "--format=custom" in args[-1]
        assert "PGUSER" in args[-1] and "PGDATABASE" in args[-1]
        assert "POSTGRES_PASSWORD" in args[-1]
        if failure != "empty":
            sys.stdout.buffer.write(b"PGDMPsynthetic-dump-secret-canary")
        if failure in {"dump", "identity"}:
            print("synthetic-dump-secret-canary", file=sys.stderr)
            sys.exit(1)
    elif args[-2:] == ["pg_restore", "--list"]:
        assert sys.stdin.buffer.read().startswith(b"PGDMP")
        print("synthetic-dump-secret-canary")
        if failure == "list":
            print("synthetic-dump-secret-canary", file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit(99)
else:
    sys.exit(99)
"""


@pytest.fixture
def backup_env(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text(DOCKER)
    (binaries / "docker").chmod(0o755)
    compose = tmp_path / "compose"
    compose.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    return {
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "COMPOSE_DIR": str(compose),
        "COMPOSE_ARGS": "-f docker-compose.yml -f host.yml",
        "BACKUP_DIR": str(backups),
        "BACKUP_KIND": "nightly",
        "BACKUP_CONTOUR": "production",
        "BACKUP_RETAIN": "2",
        "COMMAND_LOG": str(tmp_path / "commands.jsonl"),
        # These new host values must never select the existing container's database.
        "POSTGRES_USER": "wrong-new-user",
        "POSTGRES_DB": "wrong-new-db",
        "POSTGRES_PASSWORD": "wrong-new-password",
    }


def run_backup(env, **overrides):
    result = subprocess.run(
        ["bash", str(SCRIPT)], env={**env, **overrides}, capture_output=True, timeout=15
    )
    if (
        CANARY.encode() in result.stdout + result.stderr
        or b"wrong-new-" in result.stdout + result.stderr
    ):
        pytest.fail("backup output contains payload or host identity", pytrace=False)
    return result


def test_verified_archive_has_private_permissions_and_metadata(backup_env):
    result = run_backup(backup_env)
    assert result.returncode == 0, result.stderr.decode()
    directory = Path(backup_env["BACKUP_DIR"])
    archives = list(directory.glob("*.dump"))
    assert len(archives) == 1
    archive = archives[0]
    assert archive.read_bytes().startswith(b"PGDMP")
    assert directory.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600
    assert str(archive).encode() in result.stdout
    assert f"bytes={archive.stat().st_size}".encode() in result.stdout
    assert b"archive_list_exit=0" in result.stdout
    assert not list(directory.glob("*.partial"))
    commands = [
        json.loads(line) for line in Path(backup_env["COMMAND_LOG"]).read_text().splitlines()
    ]
    assert sum(command[-2:] == ["pg_restore", "--list"] for command in commands) == 1
    assert all("wrong-new-" not in str(command) for command in commands)


@pytest.mark.parametrize(
    "failure", ["connect", "missing", "stopped", "identity", "dump", "empty", "list"]
)
def test_failure_publishes_nothing_and_does_not_rotate(backup_env, failure):
    directory = Path(backup_env["BACKUP_DIR"])
    existing = directory / "orchestrator_nightly_20260901T030000000000000Z_abcdefgh.dump"
    existing.write_bytes(b"previous-good")
    result = run_backup(backup_env, FAILURE=failure, BACKUP_RETAIN="1")
    assert result.returncode != 0
    assert list(directory.glob("*.dump")) == [existing]
    assert existing.read_bytes() == b"previous-good"
    assert not list(directory.glob("*.partial"))
    assert b"archive_list_exit=0" not in result.stdout


@pytest.mark.parametrize(
    "name",
    ["COMPOSE_DIR", "COMPOSE_ARGS", "BACKUP_DIR", "BACKUP_KIND", "BACKUP_CONTOUR", "BACKUP_RETAIN"],
)
def test_required_configuration_has_no_fallback(backup_env, name):
    env = dict(backup_env)
    env.pop(name)
    assert run_backup(env).returncode != 0
    assert not list(Path(backup_env["BACKUP_DIR"]).glob("*.dump"))


@pytest.mark.parametrize("retain", ["0", "-1", "a", "1.5", "999999999999999999999", "02"])
def test_retention_policy_is_validated_before_dump(backup_env, retain):
    assert run_backup(backup_env, BACKUP_RETAIN=retain).returncode != 0
    assert not Path(backup_env["COMMAND_LOG"]).exists()


def test_bad_destination_fails_before_dump(backup_env, tmp_path):
    destination = tmp_path / "file"
    destination.write_text("cannot write an archive here")
    assert run_backup(backup_env, BACKUP_DIR=str(destination)).returncode != 0
    assert not Path(backup_env["COMMAND_LOG"]).exists()


def test_unwritable_parent_cannot_publish_or_rotate(backup_env, tmp_path):
    parent = tmp_path / "read-only"
    parent.mkdir(mode=0o500)
    try:
        assert run_backup(backup_env, BACKUP_DIR=str(parent / "new")).returncode != 0
        assert not Path(backup_env["COMMAND_LOG"]).exists()
    finally:
        parent.chmod(0o700)


def test_nightly_retention_preserves_legacy_and_protected_artifacts(backup_env):
    directory = Path(backup_env["BACKUP_DIR"])
    protected = [
        "orchestrator_2026-09-01.sql.gz",
        "before-upgrade.dump",
        "orchestrator_predeploy_42.dump",
        "orchestrator_nightly_manual.dump",
    ]
    for name in protected:
        (directory / name).write_bytes(b"protected")
    for _ in range(4):
        assert run_backup(backup_env).returncode == 0
    nightly = list(directory.glob("orchestrator_nightly_*Z_*.dump"))
    assert len(nightly) == 2
    assert all((directory / name).read_bytes() == b"protected" for name in protected)


def test_concurrent_predeploy_reruns_have_distinct_names_and_no_rotation(backup_env):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: run_backup(
                    backup_env, BACKUP_KIND="predeploy", BACKUP_LABEL="run-42-attempt-2"
                ),
                range(3),
            )
        )
    assert all(result.returncode == 0 for result in results)
    archives = list(Path(backup_env["BACKUP_DIR"]).glob("*.dump"))
    assert len(archives) == 3
    assert all("run-42-attempt-2" in archive.name for archive in archives)


def test_only_absent_stand_database_can_bootstrap(backup_env):
    result = run_backup(
        backup_env,
        BACKUP_KIND="predeploy",
        BACKUP_LABEL="run-42",
        BACKUP_CONTOUR="stand",
        FAILURE="missing",
    )
    assert result.returncode == 0
    assert b"empty_stand_bootstrap" in result.stdout
    assert b"archive_list_exit=0" not in result.stdout
    assert (
        run_backup(
            backup_env,
            BACKUP_KIND="predeploy",
            BACKUP_LABEL="run-42",
            BACKUP_CONTOUR="stand",
            FAILURE="stopped",
        ).returncode
        != 0
    )


def test_first_party_workflow_runners_are_pinned():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", path
