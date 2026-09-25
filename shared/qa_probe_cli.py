"""Stdlib-only CLI for typed calls to a run-scoped QA capability endpoint."""

from __future__ import annotations

__all__ = [
    "CAPABILITIES_CALL",
    "QA_PROBE_NAME",
    "QA_PROBE_PATH",
    "QA_PROBE_SCRIPT",
    "QA_PROBE_USAGE",
    "SUBMIT_VERDICT_CALL",
    "TELEGRAM_IDENTITY_CALL",
    "TELEGRAM_IDENTITY_FILE",
]

QA_PROBE_NAME = "qa"
QA_PROBE_PATH = f"/workspace/{QA_PROBE_NAME}"

# Shared names for the endpoint and injected script.
CAPABILITIES_CALL = "capabilities"
SUBMIT_VERDICT_CALL = "submit_qa_result"
# The QA Telegram account's Telethon credentials, served only after the QA
# runtime proved them for this run. The CLI writes them to a private file in the
# container's home (never the host-mounted /workspace) and prints only the path.
TELEGRAM_IDENTITY_CALL = "telegram_identity"
TELEGRAM_IDENTITY_FILE = "~/.qa/telegram_identity.json"

QA_PROBE_USAGE = """\
qa capabilities                     — what this run may reach
qa http_get PATH                    — GET a path on the deployed public URL
qa localhost_http_get PORT PATH     — GET a path on the target's loopback
qa remote_read PATH                 — read a file in the deployment directory
qa remote_exec ARG [ARG ...]        — one read-only docker call, e.g.
                                      qa remote_exec docker top <container>
qa container_logs CONTAINER [TAIL]  — tail one container's log
qa container_inspect CONTAINER      — one container's state
qa fire_job NAME                    — invoke one named scheduled behaviour
qa job_evidence NAME                — read back this run's record of that fire
qa telegram_probe MESSAGE           — send a message to the bot under test
qa telegram_click_button ID DATA    — invoke a visible inline bot button
qa telegram_identity                — write the QA Telegram account's Telethon
                                      credentials and proxy to ~/.qa/telegram_identity.json
                                      for your own client; never print that file
qa probe PLATFORM NAME FILE [ARG ...] — run a .py or .sh product check and retain
                                      its source, arguments and output as evidence
qa report FILE                      — store the Markdown QA report
qa finish FILE                      — submit the final result JSON and end the run\
"""

# Source text runs in the executor container without this repository installed.
QA_PROBE_SCRIPT = '''#!/usr/bin/env python3
"""qa — this container's calls to the QA runtime: the target's SSH-side reads and the verdict."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USAGE = """__QA_PROBE_USAGE__"""

TIMEOUT = 180
PROBE_TIMEOUT = 60
PROBE_TEXT_MAX = 19000
PROBE_TRUNCATION_MARKER = "\\n...[truncated by qa probe CLI]"
IDENTITY_FILE = "__QA_IDENTITY_FILE__"


def fail(message):
    sys.stderr.write(message.rstrip() + "\\n")
    raise SystemExit(2)


def decode_output(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def read_file(path):
    try:
        with open(path, "rb") as handle:
            return decode_output(handle.read())
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
    if command == "fire_job":
        if len(rest) != 1:
            fail("usage: qa fire_job NAME")
        return "fire_job", {"name": rest[0]}
    if command == "job_evidence":
        if len(rest) != 1:
            fail("usage: qa job_evidence NAME")
        return "job_evidence", {"name": rest[0]}
    if command == "telegram_probe":
        if not rest:
            fail("usage: qa telegram_probe MESSAGE")
        return "telegram_probe", {"message": " ".join(rest)}
    if command == "telegram_click_button":
        if len(rest) != 2 or not rest[0].isdigit():
            fail("usage: qa telegram_click_button MESSAGE_ID CALLBACK_DATA")
        return "telegram_click_button", {"message_id": int(rest[0]), "callback_data": rest[1]}
    if command == "telegram_identity":
        if rest:
            fail("usage: qa telegram_identity")
        return "telegram_identity", {}
    if command == "probe":
        if len(rest) < 3:
            fail("usage: qa probe PLATFORM NAME FILE [ARG ...]")
        platform, name, path = rest[:3]
        if platform not in ("telegram", "http", "web"):
            fail("PLATFORM must be one of telegram, http, web")
        if not name.strip():
            fail("NAME must not be empty")
        return "probe", {"platform": platform, "name": name, "path": path, "arguments": rest[3:]}
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
    if tool == "probe":
        return run_probe(args)
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

    answer = json.loads(body)
    if tool == "telegram_identity" and not answer.get("error"):
        return write_identity(answer)
    sys.stdout.write(body + "\\n")
    error = answer.get("error")
    return 1 if isinstance(error, str) and error.strip() else 0


def run_probe(args):
    """Run one local check, then send its complete bounded record to the runner."""
    path = args["path"]
    source = read_file(path)
    if path.endswith(".py"):
        command = [sys.executable, path, *args["arguments"]]
    elif path.endswith(".sh"):
        command = ["sh", path, *args["arguments"]]
    else:
        fail("qa probe accepts only .py and .sh files")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=False,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
        stdout, stderr, exit_status = (
            decode_output(completed.stdout),
            decode_output(completed.stderr),
            completed.returncode,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = decode_output(exc.stdout)
        stderr = decode_output(exc.stderr) + "\\nprobe timed out after %ss" % PROBE_TIMEOUT
        exit_status = 124
    duration_ms = int((time.monotonic() - started) * 1000)
    source, source_truncated = bounded(source)
    stdout, stdout_truncated = bounded(stdout)
    stderr, stderr_truncated = bounded(stderr)
    record = {
        "platform": args["platform"],
        "name": args["name"],
        "source": source,
        "source_truncated": source_truncated,
        "arguments": args["arguments"],
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "exit_status": exit_status,
        "duration_ms": duration_ms,
    }
    answer = call("record_probe", record)
    if answer.get("error"):
        sys.stdout.write("probe id unavailable: %s\\n" % answer["error"])
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        return exit_status
    sys.stdout.write(str(answer.get("id", "probe id unavailable")) + "\\n")
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return exit_status


def bounded(value):
    value = decode_output(value)
    if len(value) <= PROBE_TEXT_MAX:
        return value, False
    return value[: PROBE_TEXT_MAX - len(PROBE_TRUNCATION_MARKER)] + PROBE_TRUNCATION_MARKER, True


def call(tool, args):
    endpoint = os.environ.get("QA_CAPABILITY_URL")
    token = os.environ.get("QA_CAPABILITY_TOKEN")
    if not endpoint or not token:
        fail(
            "this container was not given a QA capability endpoint; "
            "there is no other way to reach the deployment"
        )
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
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if status is None:
                status = 200
            body = decode_output(response.read())
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = decode_output(exc.read())
    except OSError as exc:
        fail("the QA capability endpoint did not answer: %s" % exc)
    try:
        answer = json.loads(body)
    except (TypeError, ValueError):
        return {
            "error": "record not retained: endpoint returned HTTP %s: %s" % (status, body[:500])
        }
    if not 200 <= status < 300 or not isinstance(answer, dict):
        return {
            "error": "record not retained: endpoint returned HTTP %s: %s" % (status, body[:500])
        }
    return answer


def write_identity(answer):
    """Keep the session out of stdout: it goes to a 0600 file, and only the path is printed."""
    proxy = urllib.parse.urlsplit(os.environ.get("HTTPS_PROXY", ""))
    identity = {
        "api_id": int(answer["api_id"]),
        "api_hash": answer["api_hash"],
        "session": answer["session"],
        "user_id": answer["user_id"],
        "proxy": ["http", proxy.hostname, proxy.port] if proxy.hostname else None,
    }
    path = os.path.expanduser(IDENTITY_FILE)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(identity, handle)
    sys.stdout.write(
        json.dumps(
            {
                "tool": "telegram_identity",
                "file": path,
                "user_id": identity["user_id"],
                "client": "TelegramClient(StringSession(f['session']), f['api_id'], "
                "f['api_hash'], proxy=tuple(f['proxy']))",
            }
        )
        + "\\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.replace("__QA_PROBE_USAGE__", QA_PROBE_USAGE).replace(
    "__QA_IDENTITY_FILE__", TELEGRAM_IDENTITY_FILE
)
