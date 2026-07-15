"""Redis-backed regressions for capability-stream ownership cleanup."""

from __future__ import annotations

import json
import subprocess
import uuid

from capability_cleanup import cleanup_owned_capability_messages, find_owned_capability_messages
import pytest


def _command(*args: str) -> str:
    name = _redis_container()
    result = subprocess.run(
        ["docker", "exec", name, "redis-cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _redis_container() -> str:
    containers = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.service=redis",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    names = [name for name in containers.stdout.splitlines() if name]
    assert len(names) == 1, "expected exactly one live Redis compose container"
    return names[0]


def _pipeline(commands: list[tuple[str, ...]]) -> None:
    payload = b"".join(
        b"*"
        + str(len(command)).encode()
        + b"\r\n"
        + b"".join(
            b"$" + str(len(value.encode())).encode() + b"\r\n" + value.encode() + b"\r\n"
            for value in command
        )
        for command in commands
    )
    result = subprocess.run(
        ["docker", "exec", "-i", _redis_container(), "redis-cli", "--pipe"],
        input=payload,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_real_redis_cleanup_handles_queued_and_pending_without_touching_foreign_message():
    stream = f"test:capability-cleanup:{uuid.uuid4().hex}"
    group = "test-cleanup"
    bindings = {stream: (group,)}
    try:
        _command("XGROUP", "CREATE", stream, group, "0", "MKSTREAM")
        owned = _command(
            "XADD", stream, "*", "data", json.dumps({"project_id": "owned", "task_id": "run-1"})
        )
        foreign = _command(
            "XADD", stream, "*", "data", json.dumps({"project_id": "foreign", "task_id": "run-2"})
        )
        _command("XREADGROUP", "GROUP", group, "consumer", "COUNT", "1", "STREAMS", stream, ">")

        assert find_owned_capability_messages(
            "owned", {"run-1"}, command=_command, bindings=bindings
        )
        cleanup_owned_capability_messages("owned", {"run-1"}, command=_command, bindings=bindings)

        assert _command("XRANGE", stream, owned, owned) == ""
        assert foreign in _command("XRANGE", stream, foreign, foreign)
        assert (
            find_owned_capability_messages("owned", {"run-1"}, command=_command, bindings=bindings)
            == []
        )
    finally:
        _command("DEL", stream)


def test_real_redis_cleanup_pages_past_foreign_pending_entries():
    stream = f"test:capability-pagination:{uuid.uuid4().hex}"
    group = "test-cleanup"
    bindings = {stream: (group,)}
    try:
        _command("XGROUP", "CREATE", stream, group, "0", "MKSTREAM")
        _pipeline(
            [
                ("XADD", stream, "*", "data", json.dumps({"project_id": f"foreign-{index}"}))
                for index in range(1000)
            ]
        )
        owned = _command("XADD", stream, "*", "data", json.dumps({"project_id": "owned"}))
        _command("XREADGROUP", "GROUP", group, "consumer", "COUNT", "1001", "STREAMS", stream, ">")

        found = find_owned_capability_messages("owned", set(), command=_command, bindings=bindings)

        assert [(message.stream, message.message_id) for message in found] == [(stream, owned)]
    finally:
        _command("DEL", stream)


def test_real_redis_cleanup_retries_after_transient_ack_failure():
    stream = f"test:capability-ack:{uuid.uuid4().hex}"
    group = "test-cleanup"
    bindings = {stream: (group,)}
    failed = False

    def transient_command(*args: str) -> str:
        nonlocal failed
        if args[0] == "XACK" and not failed:
            failed = True
            raise RuntimeError("temporary Redis ACK failure")
        return _command(*args)

    try:
        _command("XGROUP", "CREATE", stream, group, "0", "MKSTREAM")
        owned = _command("XADD", stream, "*", "data", json.dumps({"project_id": "owned"}))
        foreign = _command("XADD", stream, "*", "data", json.dumps({"project_id": "foreign"}))
        _command("XREADGROUP", "GROUP", group, "consumer", "COUNT", "2", "STREAMS", stream, ">")

        with pytest.raises(RuntimeError, match="temporary Redis ACK failure"):
            cleanup_owned_capability_messages(
                "owned", set(), command=transient_command, bindings=bindings
            )

        assert _command("XRANGE", stream, owned, owned)
        cleanup_owned_capability_messages("owned", set(), command=_command, bindings=bindings)
        assert _command("XRANGE", stream, owned, owned) == ""
        assert foreign in _command("XRANGE", stream, foreign, foreign)
    finally:
        _command("DEL", stream)
