from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Mapping
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Any

import httpx
import structlog
import yaml

from shared.clients.github import GitHubAppClient
from shared.contracts.env_contract import merge_env_contract_fragments
from shared.deployment_cleanup import REMOTE_CLEANUP_SCRIPT, build_remote_cleanup_command
from shared.generated_contracts import (
    CONTRACT_ABSENT,
    GENERATED_CONTRACT_READ_LIMIT,
    validate_generated_contract_path,
)
from shared.provisioning_policy import (
    BITLAUNCH_PROVIDER,
    authorize_run_owned_target,
    provider_operation_is_authorized,
)

GITHUB_ORG = "project-factory-organization"
ENV_CONTRACT_FILENAME = "env.contract.yaml"
ENV_CONTRACT_PROBE_MARKER = "ENV_CONTRACT_PROBE:"
STORY_BRANCH_PROBE_MARKER = "STORY_BRANCH_PROBE:"
STORY_BRANCH_DIFF_MARKER = "STORY_BRANCH_DIFF:"
STORY_BRANCH_BASE_PROBE_MARKER = "STORY_BRANCH_BASE_PROBE:"
MAIN_HEAD_PROBE_MARKER = "MAIN_HEAD_PROBE:"
MERGE_FILE_SET_PROBE_MARKER = "MERGE_FILE_SET_PROBE:"
# How the `story-branch-diff` probe chose what to compare the branch against.
# The choice is a fact of the payload rather than an assumption of its reader:
# a branch whose story has already merged is compared against something else
# than an unmerged one, and the artifact says which.
MERGE_BASE_REFERENCE = "merge_base"
PRE_MERGE_DEFAULT_HEAD_REFERENCE = "pre_merge_default_head"
MERGE_BASE_IS_HEAD_REFERENCE = "merge_base_is_head"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
# Read-only counterpart of the cleanup script: the live suite's last look at a
# deployment target before its own teardown removes the containers.
REMOTE_DIAGNOSTICS_SCRIPT = Path(__file__).with_name("live_harness_remote_diagnostics.sh")
# Bounds the snapshot the remote script may return, so an unbounded application
# log cannot become an unbounded artifact. The script bounds each container's
# tail as well; this is the ceiling for the whole thing.
DIAGNOSTICS_MAX_CHARS = 200_000

# How the read-only artifact probe below labels what it found. A file that is
# there is announced and printed; a file that is not there is announced as
# absent. The two are different answers about the product and are never merged:
# a read that failed is neither, and fails the probe instead.
PACKAGE_CONTRACT_FILE_MARKER = "PACKAGE_CONTRACT_FILE:"
PACKAGE_CONTRACT_ABSENT_MARKER = "PACKAGE_CONTRACT_ABSENT:"
# The same ceiling central QA reads a generated contract under
# (`agents.qa.packages.CONTRACT_READ_LIMIT`): a generated contract is bigger
# than a probe answer, and a truncated one must be refused rather than half-read.
PACKAGE_CONTRACT_MAX_BYTES = GENERATED_CONTRACT_READ_LIMIT
QA_DOCKER_WRAPPER = "/usr/local/bin/qa-docker"

# The run-scoped residue probe. One marker, one payload, one exec: the three
# kinds the Definition of Done names outside the control host — the deployment
# target, the image registry and the GitHub repository — are all read from this
# container, and reading them one at a time would be three docker execs and
# three SSH key fetches for the same answer.
RUN_RESIDUE_MARKER = "RUN_RESIDUE:"
# Each kind reports its own outcome, because "asked and found nothing" and
# "could not ask" have to stay apart per kind: an unreadable registry must not
# render the target scan as unaskable, and it must never render it as clean.
RESIDUE_ERROR_KEY = "error"
RESIDUE_FINDINGS_KEY = "findings"


class _CleanupServerPolicyAdapter:
    """Adapt an API server DTO to the authoritative provisioning policy."""

    def __init__(self, server: Mapping[str, Any]) -> None:
        self._server = server

    @property
    def is_managed(self) -> bool:
        return self._server.get("is_managed") is True

    @property
    def provider_id(self) -> str | None:
        value = self._server.get("provider_id")
        return value if isinstance(value, str) else None

    @property
    def provider(self) -> str | None:
        value = self._server.get("provider")
        return value if isinstance(value, str) else None

    @property
    def labels(self) -> dict:
        value = self._server.get("labels")
        return value if isinstance(value, dict) else {}


def cleanup_target_skip_reason(server: object) -> str | None:
    """Return why an API row is not a managed cleanup target, if any.

    Cleanup uses the same fail-closed admission as provisioning, and reaches it
    the same way for each provider: a Time4VPS row through the configured
    provider-ID allowlist, a BitLaunch row through the run-ownership proof its
    contour stamped on it. This decision runs before a key request, SSH, residue
    scan, or teardown, so inventory-only installation hosts cannot be contacted
    merely because they appear in the API listing.

    A BitLaunch machine cannot be allowlisted — the run that destroys it also
    created it, minutes earlier — so asking the provider-wide policy about one
    refused every target the contour had just made, and the harness reported
    only `no managed target for an owned deploy`. The run tag is the authority
    here, and it is narrower than an allowlist: it admits this run's machines
    and nothing else on the account.
    """
    if not isinstance(server, Mapping):
        return "malformed_server_record"
    adapter = _CleanupServerPolicyAdapter(server)
    if not adapter.is_managed:
        return "is_not_managed"
    if adapter.provider == BITLAUNCH_PROVIDER:
        if authorize_run_owned_target(adapter, run_tag=os.environ.get("STAND_RUN_TAG")) is None:
            return "not_owned_by_this_run"
        return None
    if not provider_operation_is_authorized(
        provider=adapter.provider,
        provider_id=adapter.provider_id,
        is_managed=adapter.is_managed,
    ):
        return "provider_not_authorized"
    return None


def managed_cleanup_targets(servers: list[object]) -> list[dict[str, Any]]:
    """Select only policy-authorized API rows and log every unrelated row."""
    logger = structlog.get_logger(__name__)
    targets: list[dict[str, Any]] = []
    for server in servers:
        reason = cleanup_target_skip_reason(server)
        if reason is not None:
            handle = server.get("handle") if isinstance(server, Mapping) else None
            logger.info("cleanup_target_skipped", server_handle=handle, reason=reason)
            continue
        # `cleanup_target_skip_reason` admits Mapping instances only.
        targets.append(dict(server))  # type: ignore[arg-type]
    return targets


def validate_managed_cleanup_target(server: Mapping[str, Any]) -> dict[str, Any]:
    """Require the non-secret connection fields of an admitted target."""
    target = dict(server)
    for field in ("handle", "ssh_user", "public_ip"):
        value = target.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"managed cleanup target has invalid {field}")
    return target


async def probe_env_contract(
    *,
    owner: str,
    repo: str,
    ref: str,
    verify_merged_into_main: bool,
    marker: str = ENV_CONTRACT_PROBE_MARKER,
) -> dict[str, Any]:
    gh = GitHubAppClient()
    paths = await gh.list_repo_files_recursive(owner, repo, ref)
    fragment_paths = sorted(p for p in paths if p.endswith(ENV_CONTRACT_FILENAME))
    fragments: list[Any] = []
    for path in fragment_paths:
        content = await gh.get_file_contents(owner, repo, path, ref)
        if content is None:
            raise RuntimeError(f"contract fragment disappeared: {path}")
        fragments.append(yaml.safe_load(content))

    contract = merge_env_contract_fragments(fragments) if fragments else None
    entries = sorted(contract.entries) if contract else []
    user_secret_entries = (
        sorted(
            key
            for key, entry in contract.entries.items()
            if getattr(entry, "source", None) == "user_secret"
        )
        if contract
        else []
    )
    required_user_secret_entries = (
        sorted(
            key
            for key, entry in contract.entries.items()
            if getattr(entry, "source", None) == "user_secret" and getattr(entry, "required", False)
        )
        if contract
        else []
    )

    merged_into_main = None
    if verify_merged_into_main:
        token = await gh.get_token(owner, repo)
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/compare/main...{ref}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            resp.raise_for_status()
            merged_into_main = resp.json()["status"] in ("identical", "behind")

    payload = {
        "ref": ref,
        "fragment_paths": fragment_paths,
        "entries": entries,
        "user_secret_entries": user_secret_entries,
        "required_user_secret_entries": required_user_secret_entries,
        "merged_into_main": merged_into_main,
    }
    print(marker + json.dumps(payload))
    return payload


async def probe_story_branch(
    *,
    owner: str,
    repo: str,
    branch: str,
    marker: str = STORY_BRANCH_PROBE_MARKER,
) -> dict[str, Any]:
    """Compare one story branch with main and print how far ahead of it it is.

    ``ahead_by`` is the whole answer the live harness needs: a story branch that
    is not ahead of main carries no commit of its own, and the PR the scheduler
    keeps trying to open for it is the 422 GitHub answers with "No commits
    between main and <branch>".
    """
    gh = GitHubAppClient()
    token = await gh.get_token(owner, repo)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/compare/main...{branch}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        comparison = resp.json()

    payload = {
        "branch": branch,
        "status": comparison["status"],
        "ahead_by": comparison["ahead_by"],
        "behind_by": comparison["behind_by"],
    }
    print(marker + json.dumps(payload))
    return payload


async def probe_story_branch_base(
    *,
    owner: str,
    repo: str,
    branch: str,
    contains_sha: str,
    marker: str = STORY_BRANCH_BASE_PROBE_MARKER,
) -> dict[str, Any]:
    """Print where one story branch was cut from, and what that commit contains.

    The second story of a project is cut in a workspace the first story left
    behind, and card 1305 is the run where it was cut from that workspace's stale
    HEAD instead of the remote default branch. The fork point is the compare's
    own ``merge_base_commit`` between the default branch and the branch, and
    whether it carries a given commit is GitHub's answer to a second comparison —
    ``identical`` or ``ahead`` from that commit — never something inferred from
    the two SHAs being different.

    One degenerate case, stated so a reader of the payload is not misled: once
    the branch has merged it is contained in the default branch, so the compare's
    merge base *is* its head and this reports the head rather than the fork
    point. The containment answer is unchanged by that — the fork point is an
    ancestor of the head, so a head that does not carry the commit is a fork
    point that does not either — and the caller asks before the merge, so the
    payload normally names the fork point itself.
    """
    gh = GitHubAppClient()
    repository = await gh.get_repo(owner, repo)
    base_ref = repository.default_branch
    token = await gh.get_token(owner, repo)
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=20) as client:
        head = await client.get(f"{api}/branches/{branch}", headers=headers)
        head.raise_for_status()
        head_sha = head.json()["commit"]["sha"]

        forward = await client.get(f"{api}/compare/{base_ref}...{branch}", headers=headers)
        forward.raise_for_status()
        merge_base = forward.json()["merge_base_commit"]["sha"]

        containment = await client.get(
            f"{api}/compare/{contains_sha}...{merge_base}", headers=headers
        )
        containment.raise_for_status()
        status = containment.json()["status"]

    payload = {
        "branch": branch,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "contains_sha": contains_sha,
        # `identical` is the fork point being that commit; `ahead` is the fork
        # point being a descendant of it. Both are containment; `behind` and
        # `diverged` are not.
        "status": status,
        "contains": status in ("identical", "ahead"),
    }
    print(marker + json.dumps(payload))
    return payload


async def probe_story_branch_diff(
    *,
    owner: str,
    repo: str,
    branch: str,
    marker: str = STORY_BRANCH_DIFF_MARKER,
) -> dict[str, Any]:
    """Print the change one story branch carries, with the head it carries it at.

    The branch is the only place a worker's work survives its container, and it
    outlives the stand: the run's own repository is on GitHub, so a failed run
    can still be shown what its worker actually wrote. The diff is read whole
    here and bounded where it is retained (`tests/live/run_evidence.py`), so one
    named constant states the bound instead of two that can disagree.

    **What the diff is taken against.** Not the default branch as it is now:
    what the branch *added*, which is the change between the commit it started
    from and its head. The two are the same thing right up to the moment the
    story merges, and then they stop being: run 34055029359 retained no diff at
    all because its story had merged by capture time, so `main...branch` was
    empty. A red run whose story merged — the QA failure this sprint has to
    produce — is exactly when the diff is worth having.

    So the reference is chosen, and named in the payload:

    * ``merge_base`` — the branch is not contained in the default branch, and
      the compare's own ``merge_base_commit`` is where it forked. This is every
      unmerged branch, and a squash- or rebase-merged one too.
    * ``pre_merge_default_head`` — the branch is contained in the default branch,
      so its merge base *is* its head and says nothing. The merge commit that
      brought it in names the default-branch commit it was merged onto, and a
      three-dot compare from there is the branch's own change again.
    * ``merge_base_is_head`` — contained, and no merge commit of it was found.
      Nothing recoverable is left to compare against; the reference is the head
      itself and the caller states that as the reason there is no diff.

    Nothing here decides whether the diff is retained: a branch that does not
    exist raises, and the caller records that as the stated reason it has no
    diff for this run.
    """
    gh = GitHubAppClient()
    repository = await gh.get_repo(owner, repo)
    base_ref = repository.default_branch
    token = await gh.get_token(owner, repo)
    headers = {"Authorization": f"token {token}"}
    json_headers = {**headers, "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=60) as client:
        head = await client.get(f"{api}/branches/{branch}", headers=json_headers)
        head.raise_for_status()
        head_sha = head.json()["commit"]["sha"]

        forward = await client.get(f"{api}/compare/{base_ref}...{branch}", headers=json_headers)
        forward.raise_for_status()
        merge_base = forward.json()["merge_base_commit"]["sha"]
        if merge_base != head_sha:
            reference, reference_kind = merge_base, MERGE_BASE_REFERENCE
        else:
            reference, reference_kind = await _reference_of_a_merged_branch(
                client,
                api=api,
                headers=json_headers,
                base_ref=base_ref,
                branch=branch,
                head_sha=head_sha,
            )

        diff = await client.get(
            f"{api}/compare/{reference}...{branch}",
            headers={**headers, "Accept": "application/vnd.github.v3.diff"},
        )
        diff.raise_for_status()

    payload = {
        "repository": f"{owner}/{repo}",
        "branch": branch,
        "head_sha": head_sha,
        "base_ref": base_ref,
        "reference": reference,
        "reference_kind": reference_kind,
        "diff": diff.text,
    }
    print(marker + json.dumps(payload))
    return payload


async def _reference_of_a_merged_branch(
    client: httpx.AsyncClient,
    *,
    api: str,
    headers: dict[str, str],
    base_ref: str,
    branch: str,
    head_sha: str,
) -> tuple[str, str]:
    """Where a branch already contained in the default branch started from.

    Read backwards: the commits the default branch has and the branch does not
    include the merge commit that brought the branch in, and that commit's other
    parent is the default-branch commit the merge was made onto. A three-dot
    compare from there has the branch's fork point as its merge base again, so
    it yields the change the branch added and nothing the default branch did
    meanwhile.

    A branch merged some other way — a fast-forward above all — leaves no such
    commit. Then there is nothing to recover and the head is returned as its own
    reference, which the caller reports as the stated reason it has no diff.
    """
    reverse = await client.get(f"{api}/compare/{branch}...{base_ref}", headers=headers)
    reverse.raise_for_status()
    for commit in reverse.json()["commits"]:
        parents = [parent["sha"] for parent in commit["parents"]]
        if head_sha in parents and len(parents) > 1:
            other = next(parent for parent in parents if parent != head_sha)
            return other, PRE_MERGE_DEFAULT_HEAD_REFERENCE
    return head_sha, MERGE_BASE_IS_HEAD_REFERENCE


async def probe_main_head(
    *,
    owner: str,
    repo: str,
    marker: str = MAIN_HEAD_PROBE_MARKER,
) -> dict[str, Any]:
    """Print the commit `main` points at, read from GitHub and nothing else.

    This exists so the live suite can say which commit the project's CI built
    without asking the deploy what it thought it was deploying. An assertion
    derived from the deploy's own input agrees with itself by construction and
    would pass on the wrong tag, which is exactly the failure it is there to
    catch (paid run 33753667796).
    """
    gh = GitHubAppClient()
    token = await gh.get_token(owner, repo)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/main",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        resp.raise_for_status()
        commit = resp.json()

    payload = {"branch": "main", "sha": commit["sha"]}
    print(marker + json.dumps(payload))
    return payload


async def _file_contents_at(
    client: httpx.AsyncClient,
    *,
    api: str,
    headers: dict[str, str],
    path: str,
    ref: str,
) -> str | None:
    """Read one UTF-8 product file at a commit, preserving an absent file as absent."""
    response = await client.get(f"{api}/contents/{path}", params={"ref": ref}, headers=headers)
    if response.status_code == HTTP_NOT_FOUND:
        return None
    response.raise_for_status()
    payload = response.json()
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise RuntimeError(f"GitHub returned no base64 content for {path} at {ref}")
    try:
        encoded = "".join(payload["content"].split())
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(
            f"GitHub returned unreadable UTF-8 content for {path} at {ref}"
        ) from error


async def probe_merge_file_set(
    *,
    owner: str,
    repo: str,
    merge_commit_sha: str,
    marker: str = MERGE_FILE_SET_PROBE_MARKER,
) -> dict[str, Any]:
    """Read the paths one story merge changed on the product's default branch.

    The commit endpoint's ``files`` are GitHub's first-parent change set for
    the deployed merge commit.  The old worker compose proxy and overwritten
    product instructions are edits, not paths of their own, so their resulting
    product files are captured beside that set while the App credential still
    exists.  Nothing here judges the result; the level-1 predicate does.
    """
    gh = GitHubAppClient()
    repository = await gh.get_repo(owner, repo)
    default_branch = repository.default_branch
    token = await gh.get_token(owner, repo)
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=30) as client:
        commit_response = await client.get(f"{api}/commits/{merge_commit_sha}", headers=headers)
        commit_response.raise_for_status()
        commit = commit_response.json()
        paths = sorted(file["filename"] for file in commit.get("files", []))
        parents = [parent["sha"] for parent in commit.get("parents", [])]

        containment = await client.get(
            f"{api}/compare/{default_branch}...{commit['sha']}", headers=headers
        )
        containment.raise_for_status()
        merged_into_default_branch = containment.json().get("status") in {"identical", "behind"}

        file_contents: dict[str, str | None] = {}
        for path in ("Makefile", "AGENTS.md"):
            if path in paths:
                file_contents[path] = await _file_contents_at(
                    client, api=api, headers=headers, path=path, ref=commit["sha"]
                )
        parent_file_contents: dict[str, str | None] = {}
        if "AGENTS.md" in paths and parents:
            parent_file_contents["AGENTS.md"] = await _file_contents_at(
                client, api=api, headers=headers, path="AGENTS.md", ref=parents[0]
            )

    payload = {
        "merge_commit_sha": commit["sha"],
        "default_branch": default_branch,
        "merged_into_default_branch": merged_into_default_branch,
        "parent_shas": parents,
        "changed_paths": paths,
        "file_contents": file_contents,
        "parent_file_contents": parent_file_contents,
    }
    print(marker + json.dumps(payload))
    return payload


async def cleanup_github_repo(*, owner: str, repo: str) -> None:
    gh = GitHubAppClient()
    token = await gh.get_org_token(owner)
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if resp.status_code not in (204, 404):
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
        verify = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Authorization": f"token {token}"},
        )
        if verify.status_code != HTTP_NOT_FOUND:
            raise RuntimeError(f"repository residue: {verify.status_code}")


def _registry_credentials() -> tuple[str, str, str]:
    registry = os.environ.get("ORCHESTRATOR_HOSTNAME")
    username = os.environ.get("REGISTRY_USER")
    password = os.environ.get("REGISTRY_PASSWORD")
    if not registry or not username or not password:
        raise RuntimeError("registry cleanup credentials are not configured")
    base = registry if registry.startswith(("http://", "https://")) else f"https://{registry}"
    return base.rstrip("/"), username, password


async def cleanup_registry_repository(*, repository: str) -> None:
    base, username, password = _registry_credentials()
    headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
    async with httpx.AsyncClient(auth=(username, password), timeout=20) as client:
        tags = await client.get(f"{base}/v2/{repository}/tags/list")
        if tags.status_code == HTTP_NOT_FOUND:
            return
        tags.raise_for_status()
        digests = set()
        for tag in tags.json().get("tags") or []:
            manifest_url = f"{base}/v2/{repository}/manifests/{tag}"
            manifest = await client.get(manifest_url, headers=headers)
            if manifest.status_code == HTTP_NOT_FOUND:
                continue
            manifest.raise_for_status()
            digest = manifest.headers.get("Docker-Content-Digest")
            if not digest:
                raise RuntimeError(f"manifest digest missing for {repository}:{tag}")
            digests.add(digest)

        for digest in digests:
            deleted = await client.delete(f"{base}/v2/{repository}/manifests/{digest}")
            if deleted.status_code not in (202, 404):
                deleted.raise_for_status()

        verify = await client.get(f"{base}/v2/{repository}/tags/list")
        if verify.status_code == HTTP_NOT_FOUND:
            return
        verify.raise_for_status()
        live_tags = []
        for tag in verify.json().get("tags") or []:
            manifest = await client.get(f"{base}/v2/{repository}/manifests/{tag}", headers=headers)
            if manifest.status_code == HTTP_NOT_FOUND:
                continue
            manifest.raise_for_status()
            live_tags.append(tag)
        if live_tags:
            raise RuntimeError(f"registry tags remain for {repository}: {live_tags}")


def build_remote_diagnostics_command(project_name: str) -> str:
    return shlex.join(["sh", "-s", "--", project_name])


def tolerant_prefix_pattern(prefix: str) -> str:
    """Match a stack-name prefix whether Docker kept or replaced its dashes."""
    return "".join("[-_]" if char == "-" else char for char in prefix)


def build_remote_residue_command(prefixes: list[str], service_base: str = "/opt/services") -> str:
    """Build the remote inventory of live-test stacks, whatever the DB knows.

    This is the global sweep's eyes on a target: it answers "what live-test
    stacks are on this host" from the host itself — containers and service
    directories — so an orphan whose DB rows a previous cleanup already deleted
    is still seen. Reports, never deletes.
    """
    base = service_base.rstrip("/")
    pattern = "|".join(tolerant_prefix_pattern(prefix) for prefix in prefixes)
    # The trailing `*` must reach the shell unquoted to glob; the prefixes are
    # slug fragments, so only the base path can need quoting.
    globs = " ".join(f"{shlex.quote(base)}/{prefix}*" for prefix in prefixes)
    # A container is only reported when its name carries the project UUID a slug
    # always ends in, so an unrelated container that merely starts like a test
    # project does not become residue.
    #
    # `docker` is read into a variable first because a pipeline's status is its
    # last command's: piping straight into `grep | sed` would let a dead daemon
    # exit 0 with no output, and this scan's only job is to never report a false
    # clean. An unreachable docker must fail the scan, not empty it.
    script = (
        "names=$(docker ps -a --format '{{.Names}}') || exit 1; "
        f"printf '%s\\n' \"$names\" | grep -E {shlex.quote(f'^({pattern})[0-9a-f]{{32}}')} "
        "| sed 's/^/container /'; "
        f"ls -1d {globs} 2>/dev/null | sed 's/^/directory /'"
    )
    return shlex.join(["sh", "-c", script])


def build_remote_run_residue_command(
    stack_names: list[str], service_base: str = "/opt/services"
) -> str:
    """Build one run's residue inventory on its deployment target.

    The counterpart of `build_remote_residue_command`, scoped to the exact stack
    names one run owns rather than to the contour's prefixes: this answers the
    Definition of Done's "no container on the target", including containers that
    exited, and the service directory the deploy wrote.

    Anchored on the stack name *and* on the separator Compose puts after it —
    a container of a stack is `<stack>-<service>-<n>` — so a run owning
    `live-test-9` is not failed by a neighbouring `live-test-90-…`. Tolerant of
    the dash Docker may have replaced with an underscore, the same tolerance the
    prefix sweep needs and for the same reason.

    Reports, never deletes, and **fails rather than reporting an empty host**: a
    `docker ps` that could not run exits non-zero here, so an unreachable daemon
    reaches the caller as a kind that could not be checked instead of as a
    target with nothing on it.
    """
    base = service_base.rstrip("/")
    pattern = "|".join(tolerant_prefix_pattern(name) for name in stack_names)
    paths = " ".join(shlex.quote(f"{base}/{name}") for name in stack_names)
    script = (
        "names=$(docker ps -a --format '{{.Names}}') || exit 1; "
        f"printf '%s\\n' \"$names\" | grep -E {shlex.quote(f'^({pattern})[-_]')} "
        "| sed 's/^/container /'; "
        f"ls -1d {paths} 2>/dev/null | sed 's/^/directory /'; "
        "exit 0"
    )
    return shlex.join(["sh", "-c", script])


def build_remote_package_contract_command(
    project_name: str, paths: list[str], docker_wrapper: str = QA_DOCKER_WRAPPER
) -> str:
    """Read fixed generated artifacts from one deployment's backend container."""
    validated = [validate_generated_contract_path(path) for path in paths]
    project_filter = f"label=com.docker.compose.project={project_name}"
    service_filter = "label=com.docker.compose.service=backend"
    script = (
        "listing=$(docker ps --no-trunc "
        f"--filter {shlex.quote(project_filter)} --filter {shlex.quote(service_filter)} "
        "--format '{{.Names}}' 2>&1); status=$?; "
        "if [ $status -ne 0 ]; then "
        "printf 'backend container lookup failed: %.300s\\n' \"$listing\" >&2; exit 10; fi; "
        "count=$(printf '%s\\n' \"$listing\" | awk 'NF { count++ } END { print count + 0 }'); "
        'if [ "$count" -ne 1 ]; then '
        "printf 'deployment backend container count is %s, expected 1\\n' \"$count\" >&2; "
        "exit 11; fi; "
        "container=$(printf '%s\\n' \"$listing\" | awk 'NF { print; exit }'); "
    )
    steps: list[str] = []
    wrapper = (
        ["sudo", "-n", docker_wrapper] if docker_wrapper == QA_DOCKER_WRAPPER else [docker_wrapper]
    )
    wrapper_command = shlex.join(wrapper)
    for path in validated:
        quoted = shlex.quote(path)
        steps.append(
            f'content=$({wrapper_command} read-contract "$container" {quoted} '
            f"{PACKAGE_CONTRACT_MAX_BYTES}); status=$?; "
            f"if [ $status -eq 0 ]; then printf '%s %s\\n' "
            f"{shlex.quote(PACKAGE_CONTRACT_FILE_MARKER)} {quoted}; "
            "printf '%s\\n' \"$content\"; "
            f"elif [ $status -eq {CONTRACT_ABSENT} ]; then printf '%s %s\\n' "
            f"{shlex.quote(PACKAGE_CONTRACT_ABSENT_MARKER)} {quoted}; "
            "else printf 'container contract read failed for %s (status %s)\\n' "
            f'{quoted} "$status" >&2; exit 12; fi'
        )
    return shlex.join(["sh", "-c", script + "; ".join(steps)])


async def read_package_contracts(
    *,
    project_name: str,
    api_url: str,
    paths: list[str],
    server_handle: str | None = None,
) -> str:
    """The deployment's own generated artifacts, read before it is torn down.

    Unlike the diagnostics snapshot, this one raises on every failure it meets.
    Its caller decides whether a product took the kit package route, and a read
    that did not happen must never be able to answer that question at all: one
    unresolved target, an ssh that did not run, a remote read that failed are
    each stated and raised rather than returned as an empty product.
    """
    targets = await _resolve_ssh_targets(api_url, server_handle)
    if len(targets) != 1:
        raise RuntimeError(
            f"the deployment of {project_name} resolves to {len(targets)} managed targets, "
            "so there is no one host its generated artifacts can be read from"
        )
    destination, key, handle = targets[0]
    remote_cmd = build_remote_package_contract_command(project_name, paths)
    result = _run_over_ssh(destination, key, remote_cmd, "", timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"the generated artifacts of {project_name} could not be read on {handle}: "
            f"ssh exited {result.returncode}: {result.stderr.strip()[:300]}"
        )
    return result.stdout


async def _github_repository_residue(*, owner: str, repo: str) -> dict[str, Any]:
    """Whether the run's GitHub repository is still there, read and never assumed.

    `cleanup_github_repo` verifies its own delete, but a removal that verifies
    itself is not the same claim as "after the whole teardown, nothing of this
    run is on GitHub": this asks again, from outside the delete, which is what
    the Definition of Done names.
    """
    gh = GitHubAppClient()
    token = await gh.get_org_token(owner)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        )
    if resp.status_code == HTTP_NOT_FOUND:
        return {RESIDUE_FINDINGS_KEY: []}
    if resp.status_code == HTTP_OK:
        return {RESIDUE_FINDINGS_KEY: [f"repository {owner}/{repo}"]}
    # Neither answer. A 403, a 500 or a rate limit says nothing about the
    # repository, and saying "absent" for it is exactly the reading card 1318
    # removed from the manager log.
    return {RESIDUE_ERROR_KEY: f"GET repos/{owner}/{repo} answered {resp.status_code}"}


async def _registry_repositories_residue(repositories: list[str]) -> dict[str, Any]:
    """Whether any image repository this run published still has a live tag."""
    try:
        base, username, password = _registry_credentials()
    except RuntimeError as error:
        return {RESIDUE_ERROR_KEY: str(error)}
    headers = {"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
    findings: list[str] = []
    async with httpx.AsyncClient(auth=(username, password), timeout=20) as client:
        for repository in repositories:
            listing = await client.get(f"{base}/v2/{repository}/tags/list")
            if listing.status_code == HTTP_NOT_FOUND:
                continue
            if listing.status_code != HTTP_OK:
                return {
                    RESIDUE_ERROR_KEY: (
                        f"GET /v2/{repository}/tags/list answered {listing.status_code}"
                    )
                }
            for tag in listing.json().get("tags") or []:
                manifest = await client.get(
                    f"{base}/v2/{repository}/manifests/{tag}", headers=headers
                )
                if manifest.status_code == HTTP_NOT_FOUND:
                    continue
                if manifest.status_code != HTTP_OK:
                    return {
                        RESIDUE_ERROR_KEY: (
                            f"GET /v2/{repository}/manifests/{tag} answered {manifest.status_code}"
                        )
                    }
                findings.append(f"{repository}:{tag}")
    return {RESIDUE_FINDINGS_KEY: findings}


async def _target_residue(
    *, stack_names: list[str], api_url: str, server_handle: str | None
) -> dict[str, Any]:
    """What is left of this run's deployment on the target host itself.

    Asked of every managed target when the manifest names no resolved host, for
    the same reason the teardown clears every one: a write-ahead deploy record
    knows its stack name and not yet its host, and a scan of the wrong host
    finds nothing and proves nothing.
    """
    if not stack_names:
        return {RESIDUE_FINDINGS_KEY: []}
    try:
        targets = await _resolve_ssh_targets(api_url, server_handle)
    except Exception as error:  # noqa: BLE001 — an unresolvable target is unaskable, not clean
        return {RESIDUE_ERROR_KEY: f"target resolution failed: {type(error).__name__}: {error}"}
    remote_cmd = build_remote_run_residue_command(stack_names)
    findings: list[str] = []
    for destination, key, handle in targets:
        try:
            result = _run_over_ssh(destination, key, remote_cmd, "", timeout=60)
        except subprocess.SubprocessError as error:
            return {RESIDUE_ERROR_KEY: f"{handle}: ssh failed: {type(error).__name__}"}
        if result.returncode != 0:
            return {
                RESIDUE_ERROR_KEY: (
                    f"{handle}: residue scan exited {result.returncode}: "
                    f"{result.stderr.strip()[:300]}"
                )
            }
        findings += [
            f"{handle}: {line.strip()}" for line in result.stdout.splitlines() if line.strip()
        ]
    return {RESIDUE_FINDINGS_KEY: findings}


async def probe_run_residue(
    *,
    owner: str,
    repo: str,
    repositories: list[str],
    stack_names: list[str],
    api_url: str,
    server_handle: str | None = None,
    marker: str = RUN_RESIDUE_MARKER,
) -> dict[str, Any]:
    """Read the three off-host kinds after a run's teardown, each on its own terms.

    One invocation for three kinds, because all three are read from this
    container and each of them alone would cost another docker exec. They stay
    three answers: a kind that could not be read carries its own `error`, so one
    unreadable source can neither hide nor be hidden by the others.
    """
    payload = {
        "github_repository": await _github_repository_residue(owner=owner, repo=repo),
        "registry_repositories": await _registry_repositories_residue(repositories),
        "target_containers": await _target_residue(
            stack_names=stack_names, api_url=api_url, server_handle=server_handle
        ),
    }
    print(marker + json.dumps(payload, sort_keys=True))
    return payload


async def _resolve_cleanup_targets(
    client: httpx.AsyncClient, server_handle: str | None
) -> list[dict[str, Any]]:
    """Return the server DTOs one deploy's teardown must clear.

    A resolved handle is admitted by the same managed-target policy as a list.
    A deploy owned write-ahead has no target yet, so teardown clears that stack
    name on every and only managed target. An empty admitted set is a failure:
    it would silently prove nothing about a stack the manifest says may exist.
    """
    if server_handle is not None:
        srv = await client.get(f"/api/servers/{server_handle}")
        if srv.status_code != HTTP_OK:
            raise RuntimeError(f"server fetch failed: {srv.status_code}")
        targets = managed_cleanup_targets([srv.json()])
        if not targets:
            raise RuntimeError(f"server {server_handle} is not a managed cleanup target")
        return [validate_managed_cleanup_target(targets[0])]

    listing = await client.get("/api/servers/")
    if listing.status_code != HTTP_OK:
        raise RuntimeError(f"server list fetch failed: {listing.status_code}")
    servers = listing.json()
    if not isinstance(servers, list):
        raise RuntimeError("server list fetch returned a non-list response")
    targets = managed_cleanup_targets(servers)
    if not targets:
        raise RuntimeError("server list fetch returned no managed target for an owned deploy")
    return [validate_managed_cleanup_target(target) for target in targets]


async def _resolve_ssh_targets(
    api_url: str, server_handle: str | None
) -> list[tuple[str, str, str]]:
    """Destination, key and handle for every managed target of one deploy.

    One resolution for both callers — the teardown that removes the deployment
    and the read-only snapshot taken just before it — so neither can grow its
    own idea of which host a run may open, or with which key.
    """
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    targets: list[tuple[str, str, str]] = []
    async with httpx.AsyncClient(base_url=api_url, timeout=10, headers=headers) as client:
        for srv in await _resolve_cleanup_targets(client, server_handle):
            handle = srv["handle"]
            resp = await client.get(f"/api/servers/{handle}/ssh-key")
            if resp.status_code != HTTP_OK:
                raise RuntimeError(f"ssh key fetch failed: {resp.status_code}")
            key = resp.json().get("ssh_key", "")
            if not isinstance(key, str) or not key.strip():
                raise RuntimeError(f"ssh key fetch failed for {handle}: empty ssh_key")
            if not key.endswith("\n"):
                key += "\n"
            # ssh_user and public_ip come from the same DTO deploy authorizes
            # against, so neither caller can target a host the key does not open.
            targets.append((f"{srv['ssh_user']}@{srv['public_ip']}", key, handle))
    return targets


def _run_over_ssh(
    destination: str, key: str, remote_command: str, remote_script: str, *, timeout: int
) -> subprocess.CompletedProcess:
    """Stream one script to a target over ssh with a short-lived key file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
        f.write(key)
        key_path = f.name
    os.chmod(key_path, 0o600)
    try:
        return subprocess.run(
            [  # noqa: S607
                "ssh",
                "-i",
                key_path,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                destination,
                remote_command,
            ],
            input=remote_script,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        os.unlink(key_path)


async def collect_server_diagnostics(
    *,
    project_name: str,
    api_url: str,
    server_handle: str | None = None,
    remote_script_path: Path = REMOTE_DIAGNOSTICS_SCRIPT,
) -> str:
    """The deployment's own container state and log tail, read before teardown.

    The counterpart of `cleanup_server_deployment`, and it must run *before* it:
    that teardown removes the containers, and `docker ps -a` cannot list a
    container that was removed rather than stopped. This is the only place the
    question "was the application down, up but unreachable, or answering
    something QA rejected" has an answer, and the answer lives on the target for
    a few minutes only.

    A target that could not be read is named in the returned text rather than
    raising: a snapshot of the hosts that answered is worth more than nothing,
    and the reader is told which host is missing from it.
    """
    remote_script = remote_script_path.read_text()
    remote_cmd = build_remote_diagnostics_command(project_name)
    sections: list[str] = []
    for destination, key, handle in await _resolve_ssh_targets(api_url, server_handle):
        sections.append(f"== target server={handle} destination={destination} ==")
        try:
            result = _run_over_ssh(destination, key, remote_cmd, remote_script, timeout=60)
        except subprocess.SubprocessError as error:
            sections.append(f"target snapshot unavailable for {handle}: {type(error).__name__}")
            continue
        if result.returncode != 0:
            sections.append(
                f"target snapshot unavailable for {handle}: ssh exited {result.returncode}: "
                f"{result.stderr.strip()[:300]}"
            )
            continue
        sections.append(result.stdout.strip())
    return "\n".join(sections)[:DIAGNOSTICS_MAX_CHARS]


async def cleanup_server_deployment(
    *,
    project_name: str,
    api_url: str,
    server_handle: str | None = None,
    remote_script_path: Path = REMOTE_CLEANUP_SCRIPT,
) -> None:
    logger = structlog.get_logger()
    targets = await _resolve_ssh_targets(api_url, server_handle)
    remote_script = remote_script_path.read_text()
    remote_cmd = build_remote_cleanup_command(project_name)
    for destination, key, handle in targets:
        result = _run_over_ssh(destination, key, remote_cmd, remote_script, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(f"cleanup ssh failed: {result.returncode} {result.stderr[:300]}")
        logger.info("cleanup_server_done", project=project_name, server=handle, ssh=destination)


async def _run(args: argparse.Namespace) -> None:
    if args.command == "env-contract-probe":
        await probe_env_contract(
            owner=args.owner,
            repo=args.repo,
            ref=args.ref,
            verify_merged_into_main=args.verify_merged_into_main,
            marker=args.marker,
        )
    elif args.command == "story-branch-probe":
        await probe_story_branch(
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            marker=args.marker,
        )
    elif args.command == "story-branch-base-probe":
        await probe_story_branch_base(
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            contains_sha=args.contains_sha,
            marker=args.marker,
        )
    elif args.command == "story-branch-diff":
        await probe_story_branch_diff(
            owner=args.owner,
            repo=args.repo,
            branch=args.branch,
            marker=args.marker,
        )
    elif args.command == "main-head-probe":
        await probe_main_head(owner=args.owner, repo=args.repo, marker=args.marker)
    elif args.command == "merge-file-set-probe":
        await probe_merge_file_set(
            owner=args.owner,
            repo=args.repo,
            merge_commit_sha=args.merge_commit_sha,
            marker=args.marker,
        )
    elif args.command == "github-cleanup":
        await cleanup_github_repo(owner=args.owner, repo=args.repo)
    elif args.command == "registry-cleanup":
        await cleanup_registry_repository(repository=args.repository)
    elif args.command == "server-cleanup":
        await cleanup_server_deployment(
            project_name=args.project_name,
            server_handle=args.server_handle,
            api_url=args.api_url,
        )
    elif args.command == "package-contract-probe":
        # stdout is the probe answer itself: the live suite parses what this
        # prints, so nothing else may be written to it.
        print(
            await read_package_contracts(
                project_name=args.project_name,
                server_handle=args.server_handle,
                api_url=args.api_url,
                paths=args.path,
            )
        )
    elif args.command == "run-residue":
        # stdout is the proof itself: the live suite parses what this prints,
        # and a missing payload is a kind that could not be checked.
        await probe_run_residue(
            owner=args.owner,
            repo=args.repo,
            repositories=args.repository or [],
            stack_names=args.stack_name or [],
            api_url=args.api_url,
            server_handle=args.server_handle,
        )
    elif args.command == "server-diagnostics":
        # stdout is the snapshot itself: the live suite redacts and retains what
        # this prints, so nothing else may be written to it.
        print(
            await collect_server_diagnostics(
                project_name=args.project_name,
                server_handle=args.server_handle,
                api_url=args.api_url,
            )
        )
    else:  # pragma: no cover - argparse rejects this
        raise RuntimeError(f"unknown command: {args.command}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("env-contract-probe")
    probe.add_argument("--owner", required=True)
    probe.add_argument("--repo", required=True)
    probe.add_argument("--ref", required=True)
    probe.add_argument("--verify-merged-into-main", action="store_true")
    probe.add_argument("--marker", default=ENV_CONTRACT_PROBE_MARKER)

    story_branch = sub.add_parser("story-branch-probe")
    story_branch.add_argument("--owner", required=True)
    story_branch.add_argument("--repo", required=True)
    story_branch.add_argument("--branch", required=True)
    story_branch.add_argument("--marker", default=STORY_BRANCH_PROBE_MARKER)

    story_branch_base = sub.add_parser("story-branch-base-probe")
    story_branch_base.add_argument("--owner", required=True)
    story_branch_base.add_argument("--repo", required=True)
    story_branch_base.add_argument("--branch", required=True)
    story_branch_base.add_argument("--contains-sha", required=True)
    story_branch_base.add_argument("--marker", default=STORY_BRANCH_BASE_PROBE_MARKER)

    story_branch_diff = sub.add_parser("story-branch-diff")
    story_branch_diff.add_argument("--owner", required=True)
    story_branch_diff.add_argument("--repo", required=True)
    story_branch_diff.add_argument("--branch", required=True)
    story_branch_diff.add_argument("--marker", default=STORY_BRANCH_DIFF_MARKER)

    main_head = sub.add_parser("main-head-probe")
    main_head.add_argument("--owner", required=True)
    main_head.add_argument("--repo", required=True)
    main_head.add_argument("--marker", default=MAIN_HEAD_PROBE_MARKER)

    merge_file_set = sub.add_parser("merge-file-set-probe")
    merge_file_set.add_argument("--owner", required=True)
    merge_file_set.add_argument("--repo", required=True)
    merge_file_set.add_argument("--merge-commit-sha", required=True)
    merge_file_set.add_argument("--marker", default=MERGE_FILE_SET_PROBE_MARKER)

    github = sub.add_parser("github-cleanup")
    github.add_argument("--owner", required=True)
    github.add_argument("--repo", required=True)

    registry = sub.add_parser("registry-cleanup")
    registry.add_argument("--repository", required=True)

    server = sub.add_parser("server-cleanup")
    server.add_argument("--project-name", required=True)
    # Optional: a write-ahead deploy record has no resolved target yet, and then
    # the stack name is cleared on every server the API lists.
    server.add_argument("--server-handle")
    server.add_argument("--api-url", required=True)

    package_contract = sub.add_parser("package-contract-probe")
    package_contract.add_argument("--project-name", required=True)
    package_contract.add_argument("--server-handle")
    package_contract.add_argument("--api-url", required=True)
    package_contract.add_argument("--path", action="append", required=True)

    run_residue = sub.add_parser("run-residue")
    run_residue.add_argument("--owner", required=True)
    run_residue.add_argument("--repo", required=True)
    run_residue.add_argument("--repository", action="append")
    run_residue.add_argument("--stack-name", action="append")
    run_residue.add_argument("--server-handle")
    run_residue.add_argument("--api-url", required=True)

    diagnostics = sub.add_parser("server-diagnostics")
    diagnostics.add_argument("--project-name", required=True)
    diagnostics.add_argument("--server-handle")
    diagnostics.add_argument("--api-url", required=True)

    return parser


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
