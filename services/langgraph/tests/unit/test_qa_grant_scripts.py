"""The grant and revoke scripts, run by a real shell against a real directory.

These are shell programs that decide what the QA runtime is allowed to say
about a target, so they are tested the way the proof script in the `qa_identity`
role is: executed, with `getent` stubbed to point at a home this test built and
the permissions on that home set to the shape under test. A test that asserted
against the script's text would have kept passing through all three paid runs
that misread a permission problem as an absent file.

The distinction under test is one thing: an account that cannot search a
directory learns nothing about what is in it. "There is no `authorized_keys`
here" is a claim about the target's provisioning, and only a connection able to
look may make it — that is what separates exit 4 from exit 5, and, on the revoke
side, an honest zero from a fail-open one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from src.consumers._qa_target import (
    _INSTALL_GRANT,
    _REVOKE_GRANT,
    IDENTITY_ABSENT,
    IDENTITY_KEYS_ABSENT,
    IDENTITY_UNREADABLE,
)

QA_USER = "qa-observer"
MARKER = "codegen-qa-run-deadbeef"
ENTRY = f'restrict,expiry-time="202609031200" ssh-ed25519 AAAARUNKEY {MARKER}'
SENTINEL = "# codegen-qa-identity: this line is not a key"

# The scripts run as whoever the fleet key opened. Under `root` every read
# succeeds, so the account that cannot look — the thing these tests are about —
# cannot be built. That is exactly the difference this card removes from the
# stand, and CI runs unit tests unprivileged.
unprivileged_only = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root can read every directory, so an unreadable home cannot be modelled",
)


@contextmanager
def _sealed(path: Path) -> Iterator[Path]:
    """`path` as a directory or file this account cannot look inside."""
    previous = path.stat().st_mode
    path.chmod(0o000)
    try:
        yield path
    finally:
        path.chmod(previous)


def _target(tmp_path: Path, *, home: bool = True, ssh: bool = True, keys: str | None = SENTINEL):
    """A target home, and the `getent` that points the script at it.

    Every shape below is one a real host can be in: no account at all, an
    account whose home was cleaned up, a home with no `.ssh`, and the
    provisioned seat itself.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    home_dir = tmp_path / "home" / QA_USER
    if home:
        home_dir.mkdir(parents=True, exist_ok=True)
        if ssh:
            (home_dir / ".ssh").mkdir(mode=0o700, exist_ok=True)
            if keys is not None:
                (home_dir / ".ssh" / "authorized_keys").write_text(keys + "\n")
    else:
        home_dir.parent.mkdir(parents=True, exist_ok=True)
    getent = stub_dir / "getent"
    getent.write_text(
        "#!/bin/sh\n"
        f'[ "$2" = {shlex.quote(QA_USER)} ] || exit 2\n'
        f'printf "%s\\n" {shlex.quote(f"{QA_USER}:x:1001:1001::{home_dir}:/bin/bash")}\n'
    )
    getent.chmod(0o755)
    return stub_dir, home_dir


def _run(stub_dir: Path, body: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", body, "_", *args],
        capture_output=True,
        text=True,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(stub_dir.parent)},
        check=False,
    )


class TestTheInstallSaysWhatItActuallyKnows:
    """Exit 4 is a statement about the host; exit 5 is a statement about the reader."""

    def test_the_run_key_is_appended_to_the_seat_provisioning_opened(self, tmp_path):
        stub_dir, home = _target(tmp_path)

        result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == 0, result.stderr
        keys = (home / ".ssh" / "authorized_keys").read_text()
        assert keys.splitlines() == [SENTINEL, ENTRY]

    def test_an_account_the_target_does_not_have_is_absent(self, tmp_path):
        stub_dir, _ = _target(tmp_path, home=False)
        (stub_dir / "getent").write_text("#!/bin/sh\nexit 2\n")
        (stub_dir / "getent").chmod(0o755)

        result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_ABSENT
        assert "no such account" in result.stderr

    def test_a_readable_home_with_no_authorized_keys_is_absent(self, tmp_path):
        stub_dir, _ = _target(tmp_path, keys=None)

        result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_KEYS_ABSENT
        assert "no authorized_keys" in result.stderr

    def test_a_readable_home_with_no_ssh_directory_is_absent(self, tmp_path):
        stub_dir, _ = _target(tmp_path, ssh=False)

        result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_KEYS_ABSENT
        assert "no .ssh" in result.stderr

    @unprivileged_only
    def test_a_home_this_connection_cannot_search_is_not_reported_absent(self, tmp_path):
        """The stand's own failure, three paid runs in a row.

        `qa_identity` creates `.ssh` 0700 owned by the QA account, so a
        non-administrative admin connection stats nothing inside the home. The
        seat was there every time.
        """
        stub_dir, home = _target(tmp_path)

        with _sealed(home):
            result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_UNREADABLE
        assert "cannot search" in result.stderr
        assert "no authorized_keys" not in result.stderr
        assert "no such account" not in result.stderr

    @unprivileged_only
    def test_an_ssh_directory_this_connection_cannot_search_is_not_reported_absent(self, tmp_path):
        stub_dir, home = _target(tmp_path)

        with _sealed(home / ".ssh"):
            result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_UNREADABLE
        assert "cannot use" in result.stderr
        assert "no authorized_keys" not in result.stderr

    @unprivileged_only
    def test_an_unwritable_authorized_keys_fails_before_the_append_does(self, tmp_path):
        """The append would have failed on permissions anyway; this says why."""
        stub_dir, home = _target(tmp_path)

        with _sealed(home / ".ssh" / "authorized_keys"):
            result = _run(stub_dir, _INSTALL_GRANT, QA_USER, ENTRY)

        assert result.returncode == IDENTITY_UNREADABLE
        assert "cannot write" in result.stderr
        assert (home / ".ssh" / "authorized_keys").read_text() == SENTINEL + "\n"


class TestTheRevokeNeverReportsAZeroItDidNotRead:
    """A cleanup that reports "no keys survive" must have read the file."""

    def test_the_run_key_is_removed_and_the_readback_is_zero(self, tmp_path):
        stub_dir, home = _target(tmp_path, keys=f"{SENTINEL}\n{ENTRY}")

        result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == "0"
        assert (home / ".ssh" / "authorized_keys").read_text().splitlines() == [SENTINEL]

    def test_a_filter_that_would_empty_the_file_leaves_it_alone(self, tmp_path):
        """The older rule, kept: an empty replacement is refused, and it shows."""
        stub_dir, home = _target(tmp_path, keys=ENTRY)

        result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == 0, result.stderr
        assert (home / ".ssh" / "authorized_keys").read_text().splitlines() == [ENTRY]
        # And the residue is reported rather than hidden: the key is still there.
        assert result.stdout.strip().splitlines()[-1] == "1"

    def test_an_absent_file_reads_back_zero_because_it_was_looked_for(self, tmp_path):
        stub_dir, _ = _target(tmp_path, keys=None)

        result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "0"

    @unprivileged_only
    def test_an_unsearchable_home_is_a_failure_and_never_a_zero(self, tmp_path):
        """The fail-open half: a revoke that cannot look must not claim a clean file."""
        stub_dir, home = _target(tmp_path, keys=f"{SENTINEL}\n{ENTRY}")

        with _sealed(home):
            result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == IDENTITY_UNREADABLE
        assert result.stdout.strip() == ""
        assert "cannot search" in result.stderr
        # And nothing was touched, so the sweep still has the same work to do.
        assert ENTRY in (home / ".ssh" / "authorized_keys").read_text()

    @unprivileged_only
    def test_an_unsearchable_ssh_directory_is_a_failure_and_never_a_zero(self, tmp_path):
        stub_dir, home = _target(tmp_path, keys=f"{SENTINEL}\n{ENTRY}")

        with _sealed(home / ".ssh"):
            result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == IDENTITY_UNREADABLE
        assert result.stdout.strip() == ""
        assert "cannot use" in result.stderr

    @unprivileged_only
    def test_an_unreadable_authorized_keys_is_a_failure_and_never_a_zero(self, tmp_path):
        stub_dir, home = _target(tmp_path, keys=f"{SENTINEL}\n{ENTRY}")

        with _sealed(home / ".ssh" / "authorized_keys"):
            result = _run(stub_dir, _REVOKE_GRANT, QA_USER, MARKER)

        assert result.returncode == IDENTITY_UNREADABLE
        assert result.stdout.strip() == ""
        assert "cannot rewrite" in result.stderr
