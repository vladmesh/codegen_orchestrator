from __future__ import annotations

import argparse
import asyncio
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

GITHUB_ORG = "project-factory-organization"
ENV_CONTRACT_FILENAME = "env.contract.yaml"
ENV_CONTRACT_PROBE_MARKER = "ENV_CONTRACT_PROBE:"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
REMOTE_CLEANUP_SCRIPT = Path(__file__).with_name("live_harness_remote_cleanup.sh")


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


def build_remote_cleanup_command(project_name: str, service_base: str = "/opt/services") -> str:
    return shlex.join(["sh", "-s", "--", project_name, service_base.rstrip("/")])


def _tolerant_prefix_pattern(prefix: str) -> str:
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
    pattern = "|".join(_tolerant_prefix_pattern(prefix) for prefix in prefixes)
    # The trailing `*` must reach the shell unquoted to glob; the prefixes are
    # slug fragments, so only the base path can need quoting.
    globs = " ".join(f"{shlex.quote(base)}/{prefix}*" for prefix in prefixes)
    # A container is only reported when its name carries the project UUID a slug
    # always ends in, so an unrelated container that merely starts like a test
    # project does not become residue.
    script = (
        f"docker ps -a --format '{{{{.Names}}}}' "
        f"| grep -E {shlex.quote(f'^({pattern})[0-9a-f]{{32}}')} "
        "| sed 's/^/container /' || true; "
        f"ls -1d {globs} 2>/dev/null | sed 's/^/directory /' || true"
    )
    return shlex.join(["sh", "-c", script])


async def _resolve_cleanup_targets(
    client: httpx.AsyncClient, server_handle: str | None
) -> list[dict[str, Any]]:
    """Return the server DTOs one deploy's teardown must clear.

    A resolved handle names its one target. A deploy owned write-ahead has no
    target yet — the allocator had not picked one when the stack name was
    recorded — so teardown clears that stack name on every server the API lists.
    An empty list is a failure, not a clean result: it would silently prove
    nothing about a stack the manifest says may exist.
    """
    if server_handle is not None:
        srv = await client.get(f"/api/servers/{server_handle}")
        if srv.status_code != HTTP_OK:
            raise RuntimeError(f"server fetch failed: {srv.status_code}")
        return [srv.json()]

    listing = await client.get("/api/servers/")
    if listing.status_code != HTTP_OK:
        raise RuntimeError(f"server list fetch failed: {listing.status_code}")
    servers = listing.json()
    if not servers:
        raise RuntimeError("server list fetch returned no target for an owned deploy")
    return servers


async def cleanup_server_deployment(
    *,
    project_name: str,
    api_url: str,
    server_handle: str | None = None,
    remote_script_path: Path = REMOTE_CLEANUP_SCRIPT,
) -> None:
    logger = structlog.get_logger()
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    targets: list[tuple[str, str, str]] = []
    async with httpx.AsyncClient(base_url=api_url, timeout=10, headers=headers) as client:
        for srv in await _resolve_cleanup_targets(client, server_handle):
            handle = srv["handle"]
            resp = await client.get(f"/api/servers/{handle}/ssh-key")
            if resp.status_code != HTTP_OK:
                raise RuntimeError(f"ssh key fetch failed: {resp.status_code}")
            key = resp.json().get("ssh_key", "")
            if not key.endswith("\n"):
                key += "\n"
            # ssh_user and public_ip come from the same DTO deploy authorizes
            # against, so teardown cannot target a host the key does not open.
            targets.append((f"{srv['ssh_user']}@{srv['public_ip']}", key, handle))

    remote_script = remote_script_path.read_text()
    remote_cmd = build_remote_cleanup_command(project_name)
    for destination, key, handle in targets:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
            f.write(key)
            key_path = f.name
        os.chmod(key_path, 0o600)
        try:
            result = subprocess.run(
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
                    remote_cmd,
                ],
                input=remote_script,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise RuntimeError(f"cleanup ssh failed: {result.returncode} {result.stderr[:300]}")
            logger.info("cleanup_server_done", project=project_name, server=handle, ssh=destination)
        finally:
            os.unlink(key_path)


async def _run(args: argparse.Namespace) -> None:
    if args.command == "env-contract-probe":
        await probe_env_contract(
            owner=args.owner,
            repo=args.repo,
            ref=args.ref,
            verify_merged_into_main=args.verify_merged_into_main,
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

    return parser


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
