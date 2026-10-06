"""The repository-level unit tests that have to read a repository document.

`make lint` (scripts/check_unit_test_reads.py) keeps every other unit test off `docs/`
and repository Markdown. These two read one because the document is the artifact:
DEPLOY.md carries the operator's executable archive readback, and
`docs/evidence/catalog-install-fixture.json` is the CI producer's record the vendored
render is checked against.
"""

import hashlib
import json
from pathlib import Path
import shlex
import subprocess

import pytest
from test_backup_rootless import invoke, rootless_client  # noqa: F401 - the fixture

from scripts.template_pin import TEMPLATE_PIN

ROOT = Path(__file__).resolve().parents[2]


def test_fixture_matches_the_retained_ci_producer_hashes():
    proof = json.loads((ROOT / "docs/evidence/catalog-install-fixture.json").read_text())
    fixture = ROOT / "shared/tests/fixtures" / TEMPLATE_PIN.fixture_dirname
    assert proof["source"] == TEMPLATE_PIN.source and proof["ref"] == TEMPLATE_PIN.ref
    assert (fixture / ".copier-answers.yml").read_text() == proof["answers"]
    actual = {
        str(path.relative_to(fixture)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    assert actual == proof["tracked_sha256"]


@pytest.mark.subprocess
def test_backup_and_independent_readback_ignore_foreign_operator_context(rootless_client):  # noqa: F811
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
