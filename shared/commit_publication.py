"""One native, non-force publisher for worker completion and explicit recovery.

Credentials are supplied by the execution owner through native Git environment
or the existing helper. This function starts no worker and writes no Git config.
"""

import os
from pathlib import Path
import re
import subprocess

from shared.contracts.dto.commit_publication import (
    SHA_PATTERN,
    CommitPublication,
    PublicationFailure,
)
from shared.diagnostics import redact_diagnostic
from shared.injected_paths import offending_paths


async def pending_publication(redis, attempt_id):
    """Recover the strict worker output retained before the SQL park write."""
    from shared.contracts.dto.commit_publication import publication_pending_key
    from shared.contracts.queues.worker_result import WorkerResultAdapter

    raw = await redis.get(publication_pending_key(attempt_id))
    if raw is None:
        return None
    result = WorkerResultAdapter.validate_json(raw)
    if getattr(result, "publication", None) is None or result.publication.attempt_id != attempt_id:
        raise ValueError("Pending publication does not own this engineering attempt")
    return result


def _inspect_commit(git, sha, baseline):
    parents = git("rev-list", "--parents", "-n", "1", sha)
    if parents.returncode:
        return PublicationFailure.INSPECTION_FAILED, parents.stderr
    changed = (
        git("diff", "--name-only", baseline or f"{sha}^", sha)
        if baseline is not None or len(parents.stdout.split()) > 1
        else git("show", "--pretty=format:", "--name-only", sha)
    )
    if changed.returncode:
        return PublicationFailure.INSPECTION_FAILED, changed.stderr
    injected = offending_paths(changed.stdout.splitlines())
    if injected:
        return PublicationFailure.INJECTED_PATHS, ", ".join(injected)
    if baseline is not None:
        ancestor = git("merge-base", "--is-ancestor", baseline, sha)
        difference = git("diff", "--quiet", baseline, sha)
        if ancestor.returncode or difference.returncode != 1:
            return PublicationFailure.NO_NEW_COMMIT, difference.stderr
    return None


def _push_and_readback(git, sha, branch, secrets):
    ref = f"refs/heads/{branch}"

    def readback():
        return git("ls-remote", "--exit-code", "--heads", "origin", ref, timeout=30)

    remote = readback()
    if remote.returncode == 0 and remote.stdout.split() == [sha, ref]:
        return CommitPublication(published=True, commit_sha=sha, branch=branch, remote_sha=sha)
    push_error = ""
    push_failure = PublicationFailure.PUSH_REFUSED
    try:
        pushed = git("push", "origin", f"{sha}:{ref}", timeout=60)
        push_error = pushed.stderr
        if not pushed.returncode:
            push_failure = PublicationFailure.READBACK_MISMATCH
    except subprocess.TimeoutExpired:
        push_failure = PublicationFailure.TIMEOUT
    remote = readback()
    words = remote.stdout.split()
    if remote.returncode == 0 and words == [sha, ref]:
        return CommitPublication(published=True, commit_sha=sha, branch=branch, remote_sha=sha)
    remote_sha = words[0] if remote.returncode == 0 and words[1:] == [ref] else None
    if remote_sha is not None and re.fullmatch(SHA_PATTERN, remote_sha) is None:
        remote_sha = None
    return CommitPublication(
        commit_sha=sha,
        branch=branch,
        remote_sha=remote_sha,
        failure=push_failure,
        stderr=redact_diagnostic(push_error + remote.stderr, secrets=secrets),
    )


def _publication_operation(
    workspace: Path,
    branch: str,
    commit: str,
    *,
    baseline: str | None = None,
    repository_url: str | None = None,
    env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
    publish: bool,
) -> CommitPublication | None:
    """Verify the named local HEAD, inspect it, push that SHA, prove the ref.

    A failed/ambiguous push still gets readback. Exact remote proof wins even
    after a lost response; a changed local HEAD never substitutes another SHA.
    """
    sha = None

    def refuse(reason, stderr=""):
        return CommitPublication(
            commit_sha=sha,
            branch=branch or None,
            failure=reason,
            stderr=redact_diagnostic(stderr, secrets=secrets),
        )

    def git(*args, timeout=15):
        return subprocess.run(  # noqa: S603 - fixed native git arguments, no shell
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=workspace,
            env=env if env is not None else os.environ.copy(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )

    try:
        if not branch or git("check-ref-format", f"refs/heads/{branch}").returncode:
            return refuse(PublicationFailure.BRANCH_MISSING)
        if repository_url is not None:
            origin = git("remote", "get-url", "origin")
            push_origin = git("remote", "get-url", "--push", "origin")
            if (
                origin.returncode
                or origin.stdout.strip() != repository_url
                or push_origin.returncode
                or push_origin.stdout.strip() != repository_url
            ):
                return refuse(PublicationFailure.WRONG_REPOSITORY)
        local_branch = git("symbolic-ref", "--short", "HEAD")
        if local_branch.returncode or local_branch.stdout.strip() != branch:
            return refuse(PublicationFailure.WRONG_BRANCH)
        resolved = git("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
        if resolved.returncode:
            return refuse(PublicationFailure.OBJECT_MISSING, resolved.stderr)
        sha = resolved.stdout.strip()
        head = git("rev-parse", "--verify", "HEAD^{commit}")
        if head.returncode or head.stdout.strip() != sha:
            return refuse(PublicationFailure.HEAD_CHANGED, head.stderr)
        inspection = _inspect_commit(git, sha, baseline)
        if inspection is not None:
            return refuse(*inspection)
        return _push_and_readback(git, sha, branch, secrets) if publish else None
    except subprocess.TimeoutExpired:
        return refuse(PublicationFailure.TIMEOUT)
    except OSError as exc:
        return refuse(PublicationFailure.INSPECTION_FAILED, str(exc))


def inspect_commit(workspace, branch, commit, *, baseline, repository_url, env):
    """Run the same local proof before the privileged owner obtains credentials."""
    return _publication_operation(
        workspace,
        branch,
        commit,
        baseline=baseline,
        repository_url=repository_url,
        env=env,
        publish=False,
    )


def publish_commit(
    workspace: Path,
    branch: str,
    commit: str,
    *,
    baseline: str | None = None,
    repository_url: str | None = None,
    env: dict[str, str] | None = None,
    secrets: tuple[str, ...] = (),
) -> CommitPublication:
    return _publication_operation(
        workspace,
        branch,
        commit,
        baseline=baseline,
        repository_url=repository_url,
        env=env,
        secrets=secrets,
        publish=True,
    )
