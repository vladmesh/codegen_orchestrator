"""Worker-mode `docker compose`, without a Docker socket and without a product edit.

A worker container has no Docker socket, so the product's compose targets can
only work through the wrapper's proxy on `localhost:9090/infra/compose`. The
orchestrator used to buy that by appending its own `worker-start` / `worker-stop`
recipes to the product's **tracked** `Makefile`, which left the edit in the
checkout — and in the product's history when the agent committed it, where the
duplicate recipes then made the product's own CI log `overriding recipe for
target worker-start`.

The kit's Makefile already has the seam this needs::

    DOCKER_COMPOSE ?= docker compose
    worker-start:
        $(DOCKER_COMPOSE) $(COMPOSE_DEV) up -d --build --wait $(svc)

`?=` means an environment value wins, so the wrapper puts `DOCKER_COMPOSE` into
the agent's environment pointing at the small program below, which lives outside
the checkout. `make worker-start` and `make worker-stop` then reach the proxy
with the same request bodies the injected recipes sent, and the product's
Makefile is never read back dirty.

The program stands in for `docker compose`, so it sees the whole command line —
including the `-f` selections the recipe passes. That is what keeps the two
modes apart: the worker-mode project (`infra/compose.base.yml` +
`infra/compose.dev.yml`) goes to the proxy, and anything else — local mode's
`compose.local.yml`, which must keep its published ports, or the integration
project — is handed to the real `docker compose` exactly as before.
"""

from pathlib import Path

#: The compose file selection `$(COMPOSE_DEV)` passes: the worker-mode project.
WORKER_COMPOSE_FILES: tuple[str, ...] = ("infra/compose.base.yml", "infra/compose.dev.yml")

#: Orchestrator-owned, outside `/workspace`: nothing here can reach a commit.
PROXY_DIRECTORY = ".codegen-worker"
PROXY_FILENAME = "docker-compose"

#: A worker-mode `up --build` builds images; the proxy answers only when Compose
#: is done, so the shim must outwait it rather than the other way round.
PROXY_TIMEOUT_SECONDS = 1800

_PROXY_BODY = r'''
import json
import os
import sys
import urllib.error
import urllib.request


def split_file_flags(argv):
    """Split a compose command line into its -f selections and everything else."""
    files = []
    rest = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-f", "--file") and index + 1 < len(argv):
            files.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith("--file="):
            files.append(arg.split("=", 1)[1])
            index += 1
            continue
        rest.append(arg)
        index += 1
    return files, rest


def run_real_compose(argv):
    """Local mode and anything else keep the Docker CLI they were written for."""
    try:
        os.execvp("docker", ["docker", "compose"] + list(argv))
    except OSError as error:
        sys.stderr.write("docker compose is unavailable in this container: %s\n" % error)
        return 1


def main(argv):
    files, rest = split_file_flags(argv)
    if files != WORKER_COMPOSE_FILES:
        return run_real_compose(argv)

    request = urllib.request.Request(
        PROXY_URL,
        data=json.dumps({"args": rest, "cwd": "."}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROXY_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        sys.stderr.write("compose proxy refused the command (HTTP %s): %s\n" % (error.code, detail))
        return 1
    except (OSError, ValueError) as error:
        sys.stderr.write("compose proxy request failed: %s\n" % error)
        return 1

    if not isinstance(body, dict):
        sys.stderr.write("compose proxy returned an unusable answer: %r\n" % (body,))
        return 1
    for stream, key in ((sys.stdout, "stdout"), (sys.stderr, "stderr")):
        text = body.get(key) or ""
        if text:
            stream.write(text if text.endswith("\n") else text + "\n")
    exit_code = body.get("exit_code")
    if not isinstance(exit_code, int):
        sys.stderr.write("compose proxy returned no exit code\n")
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def proxy_script(port: int) -> str:
    """The stand-in program, with this wrapper's proxy address compiled in."""
    header = (
        "#!/usr/bin/env python3\n"
        '"""Stands in for `docker compose` in a worker container.\n\n'
        "Written by the worker wrapper outside the product checkout; see\n"
        'worker_wrapper/compose_proxy.py."""\n'
        f"PROXY_URL = {f'http://localhost:{port}/infra/compose'!r}\n"
        f"WORKER_COMPOSE_FILES = {list(WORKER_COMPOSE_FILES)!r}\n"
        f"PROXY_TIMEOUT_SECONDS = {PROXY_TIMEOUT_SECONDS!r}\n"
    )
    return header + _PROXY_BODY


def install_compose_proxy(port: int) -> str:
    """Write the stand-in program and return the `DOCKER_COMPOSE` value for it.

    Raises:
        RuntimeError: the program could not be written, so worker-mode compose
            targets would silently fall back to a Docker socket that is absent.
    """
    directory = Path.home() / PROXY_DIRECTORY
    path = directory / PROXY_FILENAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(proxy_script(port), encoding="utf-8")
        path.chmod(0o755)
    except OSError as exc:
        raise RuntimeError(f"Could not install the worker compose proxy at {path}") from exc
    return str(path)


def compose_proxy_supported(makefile: str) -> bool:
    """Whether a product Makefile routes compose through `$(DOCKER_COMPOSE)`.

    The pinned kit does, and `scripts/template_pin.py` is what changes that pin.
    A product that stops doing it would run worker-mode targets against a Docker
    socket the container does not have, so it is refused rather than guessed at.
    """
    try:
        content = Path(makefile).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "$(DOCKER_COMPOSE)" in content


#: Handed to the agent's subprocess environment; `?=` in the kit's Makefile makes
#: an environment value win over the file's own `docker compose`.
COMPOSE_COMMAND_ENV = "DOCKER_COMPOSE"
