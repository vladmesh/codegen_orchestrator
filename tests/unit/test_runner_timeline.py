"""The runner proof timeline records every stage and executor command with its start and end."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from tests.runner import timeline


class Proof:
    def __init__(self) -> None:
        self.timeline = timeline.Timeline([])

    def select(self, name: str) -> str:
        return f"selected {name}"

    async def install(self, product: str, name: str) -> str:
        return f"{name} in {product}"

    def fail(self) -> None:
        raise RuntimeError("stage failed")


Proof.select = timeline.stage("select_payload", Proof.select, 0)
Proof.install = timeline.stage("install", Proof.install, 1)
Proof.fail = timeline.stage("scenario", Proof.fail)


def test_sync_and_async_stages_keep_their_result_and_are_timed_in_order():
    proof = Proof()

    assert proof.select("tg-channels") == "selected tg-channels"
    assert asyncio.run(proof.install("product", "reminders")) == "reminders in product"

    first, second = proof.timeline.entries
    assert (first["kind"], first["name"], first["detail"]) == (
        "stage",
        "select_payload",
        "tg-channels",
    )
    assert (second["name"], second["detail"], second["outcome"]) == (
        "install",
        "reminders",
        "passed",
    )
    for entry in (first, second):
        started = datetime.fromisoformat(entry["started_at"])
        assert datetime.fromisoformat(entry["finished_at"]) >= started
        assert entry["seconds"] >= 0 and entry["thread"] == "MainThread"


def test_a_failed_stage_is_recorded_as_failed_and_still_raises():
    proof = Proof()

    with pytest.raises(RuntimeError, match="stage failed"):
        proof.fail()

    [entry] = proof.timeline.entries
    assert entry["name"] == "scenario" and entry["outcome"] == "failed"
    assert "detail" not in entry and entry["finished_at"]


def test_executor_commands_are_timed_while_open_and_the_original_is_restored():
    calls = []

    async def run_cmd(argv, cwd=None):
        calls.append((argv, cwd))
        return 0, "out", ""

    module = SimpleNamespace(_run_cmd=run_cmd)
    recorded = timeline.Timeline([])

    with timeline.commands(recorded, module, " ".join):
        assert asyncio.run(module._run_cmd(["uv", "sync"], cwd="/w")) == (0, "out", "")

    assert module._run_cmd is run_cmd
    assert calls == [(["uv", "sync"], "/w")]
    [entry] = recorded.entries
    assert (entry["kind"], entry["name"], entry["outcome"]) == ("executor", "uv sync", "passed")
