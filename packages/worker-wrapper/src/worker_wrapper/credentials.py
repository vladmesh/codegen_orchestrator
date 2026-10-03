"""Native git and gh entrypoints. No credential survives an invocation."""

from http import HTTPStatus
import os
import subprocess
import sys
from typing import TextIO
from urllib.parse import urlsplit

import httpx

from shared.contracts.worker_control_plane import GitHubCredentialRequest, GitHubCredentialResponse


def request_token(repository: str) -> str:
    request = GitHubCredentialRequest(repository=repository)
    url = os.environ["WORKER_BROKER_URL"].rstrip("/")
    worker_id = os.environ["WORKER_ID"]
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.post(
            f"{url}/v1/workers/{worker_id}/github/credential",
            headers={"X-Worker-Broker-Token": os.environ["WORKER_BROKER_TOKEN"]},
            json=request.model_dump(mode="json"),
        )
    if response.status_code != HTTPStatus.OK:
        raise RuntimeError("repository credential refused")
    return GitHubCredentialResponse.model_validate(response.json()).token.get_secret_value()


def git_credential(operation: str, source: TextIO, output: TextIO) -> int:
    if operation in {"store", "erase"}:
        return 0
    if operation != "get":
        raise ValueError("unsupported credential operation")
    fields = {}
    for line in source:
        if line == "\n":
            break
        key, sep, value = line.rstrip("\n").partition("=")
        if not sep or key in fields:
            raise ValueError("malformed credential input")
        fields[key] = value
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        raise ValueError("unsupported credential origin")
    repository = GitHubCredentialRequest(repository=fields["path"].removesuffix(".git")).repository
    token = request_token(repository)
    output.write(f"username=x-access-token\npassword={token}\n\n")
    return 0


def repository_from_origin() -> str:
    result = subprocess.run(
        ["/usr/bin/git", "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    url = urlsplit(result.stdout.strip())
    if url.scheme != "https" or url.netloc != "github.com" or url.query or url.fragment:
        raise ValueError("unsupported repository origin")
    return GitHubCredentialRequest(
        repository=url.path.removeprefix("/").removesuffix(".git")
    ).repository


def gh_command(args: list[str]) -> int:
    # Native auth writes persistent configuration or prints the token. Workers
    # authenticate each command here instead, including --repo invocations.
    if "auth" in args:
        raise ValueError("persistent gh authentication is unavailable in workers")
    environment = {
        key: value for key, value in os.environ.items() if key not in {"GITHUB_TOKEN", "GH_TOKEN"}
    }
    if args in (["--version"], ["version"], ["--help"], ["help"]):
        return subprocess.run(
            ["/usr/lib/codegen/gh", *args], env=environment, check=False
        ).returncode
    token = request_token(repository_from_origin())
    environment["GH_TOKEN"] = token
    environment["GH_PROMPT_DISABLED"] = "1"
    return subprocess.run(["/usr/lib/codegen/gh", *args], env=environment, check=False).returncode


def git_main() -> None:
    try:
        code = git_credential(sys.argv[1], sys.stdin, sys.stdout)
    except Exception:  # noqa: BLE001 - token/protocol errors never reach logs
        sys.stderr.write("repository credential unavailable\n")
        code = 1
    raise SystemExit(code)


def gh_main() -> None:
    try:
        code = gh_command(sys.argv[1:])
    except Exception:  # noqa: BLE001 - no credential-bearing exception output
        sys.stderr.write("repository credential unavailable\n")
        code = 1
    raise SystemExit(code)
