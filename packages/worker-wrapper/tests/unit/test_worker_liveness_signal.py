"""A bounded turn keeps its partial transcript and kills its whole process tree."""

import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import AgentTurnLimitExceeded, WorkerWrapper

from shared.contracts.queues.worker_result import WorkerFailedResult, WorkerStopReason


def _make_wrapper(**overrides) -> tuple[WorkerWrapper, MagicMock]:
    defaults = {
        "broker_url": "http://worker-broker:8001",
        "broker_token": "x" * 43,
        "worker_id": "dev-liveness-1",
        "agent_type": "noop",
    }
    defaults.update(overrides)
    broker = MagicMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    broker.clear_session = AsyncMock()
    broker.update_status = AsyncMock()
    broker.submit_output = AsyncMock()
    broker.compose = AsyncMock()
    wrapper = WorkerWrapper(config=WorkerWrapperConfig(**defaults), broker_client=broker)
    return wrapper, broker


class TestTheLimitKeepsTheWork:
    @staticmethod
    def _hanging_process(partial_stdout: bytes, *, pid: int = 4321):
        """An agent that writes, then never exits until it is killed."""
        killed = asyncio.Event()

        async def communicate():
            await killed.wait()
            return partial_stdout, b"partial stderr"

        proc = MagicMock()
        proc.communicate = communicate
        proc.returncode = -9
        proc.pid = pid
        proc.kill = MagicMock(side_effect=lambda: killed.set())
        proc.wait = AsyncMock(return_value=-9)
        return proc, killed

    @pytest.mark.asyncio
    async def test_partial_transcript_survives_the_limit(self, tmp_path):
        """An hour of real output is not thrown away because the turn ran out."""
        wrapper, _ = _make_wrapper(subprocess_timeout_seconds=1, transcript_dir=str(tmp_path))
        partial = b"ran the unit suite, ran the integration suite, writing the report"
        proc, killed = self._hanging_process(partial)

        async def fake_exec(*args, **kwargs):
            return proc

        with (
            patch("asyncio.create_subprocess_exec", fake_exec),
            patch("worker_wrapper.wrapper.PARTIAL_OUTPUT_GRACE_SECONDS", 5),
            patch(
                "worker_wrapper.wrapper.os.killpg", side_effect=lambda *_: killed.set()
            ) as killpg,
            pytest.raises(AgentTurnLimitExceeded) as raised,
        ):
            await asyncio.wait_for(
                wrapper.execute_agent({"request_id": "req-1", "prompt": "write the report"}),
                timeout=10,
            )

        assert raised.value.limit_seconds == 1
        assert raised.value.stop_reason is WorkerStopReason.AGENT_LIMIT_EXCEEDED

        transcripts = list(Path(tmp_path).rglob("*"))
        written = [p for p in transcripts if p.is_file()]
        assert written, "the partial transcript must be on disk after the limit"
        body = written[0].read_text(encoding="utf-8")
        assert partial.decode() in body
        assert wrapper._transcript_path is not None
        killpg.assert_called_once_with(proc.pid, __import__("signal").SIGKILL)

    @pytest.mark.asyncio
    @pytest.mark.skipif(os.name != "posix", reason="process groups are a POSIX contract")
    async def test_process_group_kill_reaches_a_real_descendant(self):
        """A real child process cannot keep the timed-out turn's pipe alive."""
        program = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None
        child_pid = int((await proc.stdout.readline()).decode())
        await WorkerWrapper._kill_agent_process_group(proc)
        for _ in range(20):
            if not Path(f"/proc/{child_pid}").exists():
                break
            await asyncio.sleep(0.01)
        assert not Path(f"/proc/{child_pid}").exists()

    @pytest.mark.asyncio
    async def test_the_result_says_the_limit_is_what_stopped_it(self, tmp_path):
        """`stop_reason` distinguishes a limit from a crash on the wire."""
        wrapper, broker = _make_wrapper(subprocess_timeout_seconds=1, transcript_dir=str(tmp_path))
        wrapper._result_event = asyncio.Event()
        wrapper._buffered_result = None
        wrapper._stop_reason = WorkerStopReason.AGENT_LIMIT_EXCEEDED
        wrapper._agent_limit_seconds = 1

        await wrapper._publish_result(
            "1-0", {}, "Agent process exceeded its 1s turn limit", "failed", None
        )

        submitted = broker.submit_output.await_args[0][1]
        assert isinstance(submitted, WorkerFailedResult)
        assert submitted.stop_reason is WorkerStopReason.AGENT_LIMIT_EXCEEDED
        assert submitted.agent_limit_seconds == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_names_no_stop_reason(self, tmp_path):
        """Only a deliberate stop carries one; a crashed CLI is not a stop."""
        wrapper, broker = _make_wrapper(transcript_dir=str(tmp_path))
        wrapper._result_event = asyncio.Event()
        wrapper._buffered_result = None

        await wrapper._publish_result("1-0", {}, "Agent process failed with code 1", "failed", None)

        submitted = broker.submit_output.await_args[0][1]
        assert submitted.stop_reason is None


class TestTheLimitIsConfigurable:
    def test_the_limit_is_not_the_old_fifteen_minute_ceiling(self):
        """The default is sized for real product work, not for a smoke test."""
        wrapper, _ = _make_wrapper()
        assert wrapper.config.subprocess_timeout_seconds >= 45 * 60

    def test_worker_manager_may_override_it(self):
        wrapper, _ = _make_wrapper(subprocess_timeout_seconds=2700)
        assert wrapper.config.subprocess_timeout_seconds == 2700
