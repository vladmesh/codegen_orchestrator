"""Offline native Git proofs, also selected by the broad check."""

import subprocess

import pytest

from shared.commit_publication import publish_commit
from shared.contracts.dto.commit_publication import PublicationFailure

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init")
    git(work, "config", "user.email", "fixture@example.test")
    git(work, "config", "user.name", "Fixture")
    git(work, "checkout", "-b", "story/fixture")
    git(work, "remote", "add", "origin", str(remote))
    (work / "app.txt").write_text("base")
    git(work, "add", "app.txt")
    git(work, "commit", "-m", "base")
    baseline = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "HEAD")
    (work / "app.txt").write_text("changed")
    git(work, "commit", "-am", "change")
    return work, remote, baseline, git(work, "rev-parse", "HEAD")


def test_exact_commit_recovery_and_lost_reply(checkout):
    work, remote, baseline, sha = checkout
    for _ in range(2):
        receipt = publish_commit(work, "story/fixture", sha, baseline=baseline)
        assert receipt.published
        assert receipt.commit_sha == sha
        assert receipt.remote_sha == sha
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == sha


def test_changed_head_cannot_substitute_preserved_commit(checkout):
    work, remote, baseline, sha = checkout
    (work / "app.txt").write_text("newer")
    git(work, "commit", "-am", "newer")
    receipt = publish_commit(work, "story/fixture", sha, baseline=baseline)
    assert not receipt.published
    assert receipt.failure is PublicationFailure.HEAD_CHANGED
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == baseline


def test_invalid_object_does_not_claim_recoverable_sha(checkout):
    work, _, baseline, _ = checkout
    receipt = publish_commit(work, "story/fixture", "f" * 40, baseline=baseline)
    assert not receipt.published
    assert receipt.commit_sha is None
    assert receipt.failure is PublicationFailure.OBJECT_MISSING


def test_no_new_commit_is_refused(checkout):
    work, _, baseline, sha = checkout
    receipt = publish_commit(work, "story/fixture", sha, baseline=sha)
    assert not receipt.published
    assert receipt.failure is PublicationFailure.NO_NEW_COMMIT


def test_non_force_refusal_keeps_both_tips(checkout, tmp_path):
    work, remote, baseline, sha = checkout
    other = tmp_path / "other"
    git(tmp_path, "clone", str(remote), str(other))
    git(other, "checkout", "story/fixture")
    git(other, "config", "user.email", "fixture@example.test")
    git(other, "config", "user.name", "Fixture")
    (other / "app.txt").write_text("foreign")
    git(other, "commit", "-am", "foreign")
    git(other, "push", "origin", "HEAD")
    foreign = git(other, "rev-parse", "HEAD")
    receipt = publish_commit(work, "story/fixture", sha, baseline=baseline)
    assert not receipt.published
    assert receipt.failure is PublicationFailure.PUSH_REFUSED
    assert receipt.commit_sha == sha
    assert receipt.stderr
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == foreign
    assert git(work, "rev-parse", "HEAD") == sha


def test_wrong_push_repository_never_receives_commit(checkout, tmp_path):
    work, remote, baseline, sha = checkout
    foreign = tmp_path / "foreign.git"
    foreign.mkdir()
    git(foreign, "init", "--bare")
    git(work, "remote", "set-url", "--push", "origin", str(foreign))
    receipt = publish_commit(work, "story/fixture", sha, repository_url=str(remote))
    assert receipt.failure is PublicationFailure.WRONG_REPOSITORY
    assert receipt.commit_sha is None
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == baseline


def test_injected_path_in_preserved_range_refuses(checkout):
    work, remote, baseline, _ = checkout
    (work / "TASK.md").write_text("agent instructions")
    git(work, "add", "TASK.md")
    git(work, "commit", "-m", "injected")
    (work / "app.txt").write_text("later")
    git(work, "commit", "-am", "later")
    receipt = publish_commit(
        work, "story/fixture", git(work, "rev-parse", "HEAD"), baseline=baseline
    )
    assert receipt.failure is PublicationFailure.INJECTED_PATHS
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == baseline


def test_refusal_stderr_is_bounded_and_redacts_owned_token(checkout):
    work, remote, baseline, sha = checkout
    sentinel = "secret-sentinel-not-a-token-format"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\necho '{sentinel}' >&2\nexit 1\n")
    hook.chmod(0o700)
    receipt = publish_commit(work, "story/fixture", sha, baseline=baseline, secrets=(sentinel,))
    assert receipt.failure is PublicationFailure.PUSH_REFUSED
    assert sentinel not in receipt.stderr
    assert "redacted" in receipt.stderr
    assert len(receipt.stderr) <= 2000


def test_readback_mismatch_is_refusal_and_lost_push_reply_is_recovered(checkout, monkeypatch):
    work, remote, baseline, sha = checkout
    native = subprocess.run

    def stale_readback(argv, **kwargs):
        if "ls-remote" in argv:
            return subprocess.CompletedProcess(
                argv, 0, f"{baseline}\trefs/heads/story/fixture\n", ""
            )
        return native(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", stale_readback)
    receipt = publish_commit(work, "story/fixture", sha, baseline=baseline)
    assert receipt.failure is PublicationFailure.READBACK_MISMATCH
    assert receipt.remote_sha == baseline
    monkeypatch.setattr(subprocess, "run", native)
    assert git(remote, "rev-parse", "refs/heads/story/fixture") == sha
    assert publish_commit(work, "story/fixture", sha, baseline=baseline).published


def test_push_timeout_after_acceptance_is_governed_by_exact_readback(checkout, monkeypatch):
    work, _, baseline, sha = checkout
    native = subprocess.run

    def lost_answer(argv, **kwargs):
        result = native(argv, **kwargs)
        if "push" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return result

    monkeypatch.setattr(subprocess, "run", lost_answer)
    assert publish_commit(work, "story/fixture", sha, baseline=baseline).published
