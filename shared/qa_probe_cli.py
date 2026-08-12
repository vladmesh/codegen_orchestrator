"""The one command a central QA executor container has for reaching a deployment.

The exploratory QA executor is a CLI coding agent now, so it has a shell — and a
shell is not a boundary. The boundary is that nothing inside the container can
address the target: no SSH key, no fleet key, no Telegram session, no container
name it did not learn from the runtime. The only route out is this script, which
posts a named call with typed arguments to the QA runtime's capability endpoint
on the management host and prints what comes back. Every check the endpoint
performs — one deployment, one physical root, its own loopback ports, its own
containers, GET only — is the same check the in-process tool set performed, and
it is performed there, where the credentials are, not here.

The script is injected into the executor's ephemeral workspace by worker-manager
and is stdlib-only on purpose: the QA image is not asked to carry a dependency
so that a QA run can happen.

`QA_PROBE_USAGE` is the same text the QA prompt shows the agent, so the prompt
and the script cannot describe different commands.
"""

from __future__ import annotations

__all__ = [
    "CAPABILITIES_CALL",
    "QA_PROBE_NAME",
    "QA_PROBE_PATH",
    "QA_PROBE_SCRIPT",
    "QA_PROBE_USAGE",
    "SUBMIT_VERDICT_CALL",
]

QA_PROBE_NAME = "qa"
QA_PROBE_PATH = f"/workspace/{QA_PROBE_NAME}"

# The two calls the endpoint answers that are not target operations: what this
# run may reach, and the run's final result. Named here because the script below
# and the endpoint that serves it must agree on them.
CAPABILITIES_CALL = "capabilities"
SUBMIT_VERDICT_CALL = "submit_qa_result"

QA_PROBE_USAGE = """\
qa capabilities                     — what this run may reach
qa http_get PATH                    — GET a path on the deployed public URL
qa localhost_http_get PORT PATH     — GET a path on the target's loopback
qa remote_read PATH                 — read a file in the deployment directory
qa remote_exec ARG [ARG ...]        — one read-only docker call, e.g.
                                      qa remote_exec docker top <container>
qa container_logs CONTAINER [TAIL]  — tail one container's log
qa container_inspect CONTAINER      — one container's state
qa telegram_probe MESSAGE           — send a message to the bot under test
qa report FILE                      — store the Markdown QA report
qa finish FILE                      — submit the final result JSON and end the run\
"""

# Kept as source text rather than a module because it runs inside the executor
# container, which has python3 and nothing of this repository.
QA_PROBE_SCRIPT = '''#!/usr/bin/env python3
"""qa — the only way this container can reach the deployment under test."""

import json
import os
import sys
import urllib.error
import urllib.request

USAGE = """__QA_PROBE_USAGE__"""

TIMEOUT = 180


def fail(message):
    sys.stderr.write(message.rstrip() + "\\n")
    raise SystemExit(2)


def read_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        fail("cannot read %s: %s" % (path, exc))


def build_call(argv):
    command = argv[0]
    rest = argv[1:]
    if command == "capabilities":
        return "capabilities", {}
    if command == "http_get":
        if len(rest) != 1:
            fail("usage: qa http_get PATH")
        return "http_get", {"path": rest[0]}
    if command == "localhost_http_get":
        if len(rest) != 2:
            fail("usage: qa localhost_http_get PORT PATH")
        if not rest[0].isdigit():
            fail("PORT must be a number")
        return "localhost_http_get", {"port": int(rest[0]), "path": rest[1]}
    if command == "remote_read":
        if len(rest) != 1:
            fail("usage: qa remote_read PATH")
        return "remote_read", {"path": rest[0]}
    if command == "remote_exec":
        if not rest:
            fail("usage: qa remote_exec ARG [ARG ...]")
        return "remote_exec", {"command": rest}
    if command == "container_logs":
        if len(rest) not in (1, 2):
            fail("usage: qa container_logs CONTAINER [TAIL]")
        args = {"container": rest[0]}
        if len(rest) == 2:
            if not rest[1].isdigit():
                fail("TAIL must be a number")
            args["tail"] = int(rest[1])
        return "container_logs", args
    if command == "container_inspect":
        if len(rest) != 1:
            fail("usage: qa container_inspect CONTAINER")
        return "container_inspect", {"container": rest[0]}
    if command == "telegram_probe":
        if not rest:
            fail("usage: qa telegram_probe MESSAGE")
        return "telegram_probe", {"message": " ".join(rest)}
    if command == "report":
        if len(rest) != 1:
            fail("usage: qa report FILE")
        return "write_qa_report", {"markdown": read_file(rest[0])}
    if command == "finish":
        if len(rest) != 1:
            fail("usage: qa finish FILE")
        return "submit_qa_result", {"result": read_file(rest[0])}
    fail("unknown command %r\\n\\n%s" % (command, USAGE))


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE + "\\n")
        return 0

    endpoint = os.environ.get("QA_CAPABILITY_URL")
    token = os.environ.get("QA_CAPABILITY_TOKEN")
    if not endpoint or not token:
        fail(
            "this container was not given a QA capability endpoint; "
            "there is no other way to reach the deployment"
        )

    tool, args = build_call(argv)
    payload = json.dumps({"tool": tool, "args": args}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        sys.stdout.write(exc.read().decode("utf-8", "replace") + "\\n")
        return 1
    except OSError as exc:
        fail("the QA capability endpoint did not answer: %s" % exc)

    sys.stdout.write(body + "\\n")
    answer = json.loads(body)
    return 1 if "error" in answer else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.replace("__QA_PROBE_USAGE__", QA_PROBE_USAGE)
