"""The pending release set: checked before the host switch, promoted only after `up`.

`scripts/release_switch.py` is the Switch's helper at both ends (.github/workflows/deploy.yml).
The live deployed records are the only truth of what was last brought up successfully:
promotion rotates them — never a pending set — and a failed attempt that never reached
promotion leaves them exactly as they were, so it cannot displace the rollback target.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.release_switch import SwitchError, check_pending, promote

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_RECORD = json.loads(
    (REPO_ROOT / "tests" / "unit" / "fixtures" / "service-release-7f93d8b7.json").read_text()
)
WORKER_CHAIN = (
    "worker-base-common",
    "worker-base-claude",
    "worker-base-factory",
    "worker-base-codex",
)
A, B, C = ("a" * 40, "b" * 40, "c" * 40)
WORKER = "deployed-worker-images.json"
SERVICE = "deployed-service-images.json"
PREVIOUS_WORKER = "previous-deployed-worker-images.json"
PREVIOUS_SERVICE = "previous-deployed-service-images.json"
OVERRIDE = "deployed-service-images.compose.yml"


def _service(git_sha: str, source_hash: str) -> str:
    record = dict(REAL_RECORD, git_sha=git_sha, source_hash=source_hash)
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _worker(git_sha: str, source_hash: str) -> str:
    record = {
        "git_sha": git_sha,
        "source_hash": source_hash,
        "images": {
            name: {"reference": f"ghcr.io/vladmesh/codegen-orchestrator/{name}@sha256:{git_sha}"}
            for name in WORKER_CHAIN
        },
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


def _override() -> str:
    lines = ["services:"]
    for name in ("api", "langgraph"):
        lines += [f'  "{name}":', f'    image: "{REAL_RECORD["images"][name]["reference"]}"']
    return "\n".join(lines) + "\n"


def _pending(root: Path, git_sha: str, source_hash: str) -> Path:
    """What a verify step of `git_sha` leaves behind."""
    pending = root / ".release-pending"
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir()
    (pending / WORKER).write_text(_worker(git_sha, source_hash))
    (pending / SERVICE).write_text(_service(git_sha, source_hash))
    (pending / OVERRIDE).write_text(_override())
    return pending


@pytest.fixture
def live(tmp_path: Path) -> Path:
    """A host whose last successful deploy was A, with Z before it."""
    live = tmp_path / "live"
    live.mkdir()
    (live / WORKER).write_text(_worker(A, "hash-a"))
    (live / SERVICE).write_text(_service(A, "hash-a"))
    (live / PREVIOUS_WORKER).write_text(_worker("f" * 40, "hash-z"))
    (live / PREVIOUS_SERVICE).write_text(_service("f" * 40, "hash-z"))
    return live


def _records(live: Path) -> dict[str, str]:
    return {
        name: (live / name).read_text()
        for name in (WORKER, SERVICE, PREVIOUS_WORKER, PREVIOUS_SERVICE)
    }


# --- check ---------------------------------------------------------------------------------


def test_a_complete_pending_set_of_the_revision_passes(live):
    check_pending(_pending(live, B, "hash-b"), B)


@pytest.mark.parametrize("missing", [WORKER, SERVICE, OVERRIDE])
def test_an_incomplete_pending_set_is_refused(live, missing):
    pending = _pending(live, B, "hash-b")
    (pending / missing).unlink()

    with pytest.raises(SwitchError, match="incomplete"):
        check_pending(pending, B)


@pytest.mark.parametrize("record", [WORKER, SERVICE])
def test_a_record_of_another_revision_is_refused(live, record):
    pending = _pending(live, B, "hash-b")
    maker = _worker if record == WORKER else _service
    (pending / record).write_text(maker(C, "hash-b"))

    with pytest.raises(SwitchError, match=f"not of {B}"):
        check_pending(pending, B)


def test_worker_and_service_records_of_different_trees_are_refused(live):
    pending = _pending(live, B, "hash-b")
    (pending / WORKER).write_text(_worker(B, "hash-other"))

    with pytest.raises(SwitchError, match="not one tree's releases"):
        check_pending(pending, B)


def test_an_override_naming_an_image_outside_the_service_release_is_refused(live):
    pending = _pending(live, B, "hash-b")
    (pending / OVERRIDE).write_text('services:\n  "api":\n    image: "ghcr.io/x/api@sha256:0bad"\n')

    with pytest.raises(SwitchError, match="beyond the pending service release"):
        check_pending(pending, B)


def test_check_runs_from_a_lone_copy_of_the_helper(live, tmp_path):
    """The Switch runs it from the pending set, before the checkout holds this revision."""
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "release_switch.py", lone)
    pending = _pending(live, B, "hash-b")

    ok = subprocess.run(
        [
            sys.executable,
            "-I",
            str(lone / "release_switch.py"),
            "check",
            "--pending",
            str(pending),
            "--revision",
            B,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    refused = subprocess.run(
        [
            sys.executable,
            "-I",
            str(lone / "release_switch.py"),
            "check",
            "--pending",
            str(pending),
            "--revision",
            C,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert ok.returncode == 0, ok.stderr
    assert refused.returncode == 1
    assert "FATAL" in refused.stderr


# --- promote -------------------------------------------------------------------------------


def test_promotion_makes_the_live_current_previous_and_the_pending_set_current(live):
    running = _records(live)
    pending = _pending(live, B, "hash-b")

    promote(pending, live, B)

    assert (live / PREVIOUS_WORKER).read_text() == running[WORKER]
    assert (live / PREVIOUS_SERVICE).read_text() == running[SERVICE]
    assert json.loads((live / WORKER).read_text())["git_sha"] == B
    assert json.loads((live / SERVICE).read_text())["git_sha"] == B
    assert not pending.exists()


def test_a_same_revision_redeploy_rotates_nothing(live):
    before = _records(live)

    promote(_pending(live, A, "hash-a"), live, A)

    assert _records(live) == before, "previous stays the rollback target"


def test_a_failed_attempt_never_displaces_the_rollback_target(live):
    """B verified but never came up; C then deploys. Previous must be A, not B."""
    running = _records(live)
    _pending(live, B, "hash-b")  # the attempt whose `up` failed: never promoted

    assert _records(live) == running, "an unpromoted attempt leaves current and previous"

    promote(_pending(live, C, "hash-c"), live, C)

    assert json.loads((live / PREVIOUS_SERVICE).read_text())["git_sha"] == A
    assert json.loads((live / PREVIOUS_WORKER).read_text())["git_sha"] == A
    assert json.loads((live / SERVICE).read_text())["git_sha"] == C


def test_a_first_deploy_has_no_previous_release(tmp_path):
    live = tmp_path / "live"
    live.mkdir()

    promote(_pending(live, B, "hash-b"), live, B)

    assert (live / PREVIOUS_SERVICE).read_text() == ""
    assert (live / PREVIOUS_WORKER).read_text() == ""
    assert json.loads((live / WORKER).read_text())["git_sha"] == B
