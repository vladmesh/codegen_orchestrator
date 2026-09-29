"""Host-only, disposable Compose PostgreSQL proof of the actual backup script."""

import os
from pathlib import Path
import subprocess
import uuid

import pytest
import structlog

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/scripts/backup-db.sh"
IMAGE = "pgvector/pgvector:0.8.6-pg16"


def command(args, *, data=None, env=None):
    result = subprocess.run(args, input=data, env=env, capture_output=True, timeout=90)
    # Never include subprocess payload or DB output in a test failure.
    if result.returncode != 0:
        pytest.fail(f"command failed: {args[:3]} exit={result.returncode}", pytrace=False)
    return result.stdout


@pytest.fixture
def database(tmp_path):
    project = f"verified-backup-{uuid.uuid4().hex[:12]}"
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  db:\n"
        f"    image: {IMAGE}\n"
        "    environment:\n"
        "      POSTGRES_USER: archive_owner\n"
        "      POSTGRES_DB: application_canary\n"
        "      POSTGRES_PASSWORD: isolated-test-password\n"
        "    tmpfs: /var/lib/postgresql/data\n"
        "    healthcheck:\n"
        "      test: [CMD-SHELL, 'pg_isready -U archive_owner -d application_canary']\n"
        "      interval: 1s\n      timeout: 3s\n      retries: 30\n"
    )
    # A second file must be consumed by the same project, just like production.
    (tmp_path / "host.yml").write_text(
        "services:\n  db:\n    labels:\n      backup-test: verified\n"
    )
    compose = [
        "docker",
        "compose",
        "--project-directory",
        str(tmp_path),
        "-p",
        project,
        "-f",
        str(tmp_path / "docker-compose.yml"),
        "-f",
        str(tmp_path / "host.yml"),
    ]
    try:
        command([*compose, "up", "-d", "--wait", "--wait-timeout", "60", "db"])
        yield tmp_path, project, compose
    finally:
        command([*compose, "down", "--volumes", "--remove-orphans"])
        assert (
            command(
                ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
            )
            == b""
        )


def test_custom_archive_restores_selected_application_and_langgraph_rows(database, capsys):
    directory, project, compose = database
    command(
        [
            *compose,
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "archive_owner",
            "-d",
            "application_canary",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        data=b"""
CREATE TABLE public.application_backup_canary (id integer PRIMARY KEY, value text NOT NULL);
INSERT INTO public.application_backup_canary VALUES (42, 'synthetic-application-canary');
CREATE SCHEMA langgraph;
CREATE TABLE langgraph.checkpoints (thread_id text PRIMARY KEY, checkpoint jsonb);
CREATE TABLE langgraph.checkpoint_blobs (thread_id text PRIMARY KEY, blob bytea);
CREATE TABLE langgraph.checkpoint_writes (thread_id text PRIMARY KEY, blob bytea);
INSERT INTO langgraph.checkpoints VALUES
  ('synthetic-thread', '{"v": 4, "canary": "synthetic-checkpoint-canary"}');
INSERT INTO langgraph.checkpoint_blobs VALUES ('synthetic-thread', decode('010203', 'hex'));
INSERT INTO langgraph.checkpoint_writes VALUES ('synthetic-thread', decode('040506', 'hex'));
""",
    )
    backups = directory / "backups"
    env = {
        "PATH": os.environ["PATH"],
        "COMPOSE_DIR": str(directory),
        "COMPOSE_ARGS": f"-p {project} -f docker-compose.yml -f host.yml",
        "BACKUP_DIR": str(backups),
        "BACKUP_KIND": "nightly",
        "BACKUP_CONTOUR": "production",
        "BACKUP_RETAIN": "2",
        # New deployment/host identity is deliberately wrong.
        "POSTGRES_USER": "new_unrelated_user",
        "POSTGRES_DB": "new_unrelated_database",
        "POSTGRES_PASSWORD": "new_unrelated_password",
    }
    metadata = command(["bash", str(SCRIPT)], env=env)
    archives = list(backups.glob("*.dump"))
    assert len(archives) == 1
    archive = archives[0]
    content = archive.read_bytes()
    if not content.startswith(b"PGDMP") or len(content) <= 1000:
        pytest.fail("archive missing custom header or unexpectedly small", pytrace=False)
    assert f"bytes={len(content)}".encode() in metadata and b"archive_list_exit=0" in metadata
    assert backups.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600
    command([*compose, "exec", "-T", "db", "pg_restore", "--list"], data=content)
    command([*compose, "exec", "-T", "db", "createdb", "-U", "archive_owner", "restored_canary"])
    command(
        [
            *compose,
            "exec",
            "-T",
            "db",
            "pg_restore",
            "--exit-on-error",
            "-U",
            "archive_owner",
            "-d",
            "restored_canary",
        ],
        data=content,
    )
    readback = command(
        [
            *compose,
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "archive_owner",
            "-d",
            "restored_canary",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        data=b"""
SELECT count(*) FROM public.application_backup_canary
  WHERE id=42 AND value='synthetic-application-canary';
SELECT count(*) FROM langgraph.checkpoints WHERE thread_id='synthetic-thread'
  AND checkpoint->>'canary'='synthetic-checkpoint-canary';
SELECT count(*) FROM langgraph.checkpoint_blobs WHERE blob=decode('010203', 'hex');
SELECT count(*) FROM langgraph.checkpoint_writes WHERE blob=decode('040506', 'hex');
""",
    )
    assert readback == b"1\n1\n1\n1\n", "restored canary counts differ"
    assert b"synthetic-" not in metadata and b"password" not in metadata
    command([*compose, "exec", "-T", "db", "dropdb", "-U", "archive_owner", "restored_canary"])
    with capsys.disabled():
        # Operational evidence only. Fixture teardown deletes the DB/container;
        # remove our synthetic archive after the readback, never production data.
        structlog.get_logger().info(
            "backup_test_readback",
            metadata=metadata.decode().strip(),
            restore_canary_counts="1,1,1,1",
            archive_list_exit=0,
            project=project,
        )
    archive.unlink()
    assert not list(backups.glob("*.dump"))
