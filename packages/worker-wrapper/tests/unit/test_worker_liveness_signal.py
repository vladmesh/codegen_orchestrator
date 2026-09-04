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

        async def wait():
            await killed.wait()
            return -9

        proc = MagicMock()
        proc.communicate = communicate
        proc.returncode = -9
        proc.pid = pid
        proc.kill = MagicMock(side_effect=lambda: killed.set())
        proc.wait = wait
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
    async def test_result_submission_stops_the_agent_before_it_can_change_head(self):
        """A success report is a completion barrier, not a race with final HEAD."""
        wrapper, _ = _make_wrapper(subprocess_timeout_seconds=60)
        wrapper._result_event = asyncio.Event()
        proc, killed = self._hanging_process(b"reported result")

        collecting = asyncio.create_task(wrapper._collect_agent_output(proc))
        await asyncio.sleep(0)
        wrapper._result_event.set()

        with (
            patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
            patch(
                "worker_wrapper.wrapper.os.killpg", side_effect=lambda *_: killed.set()
            ) as killpg,
        ):
            stdout, stderr, limit_exceeded, stopped_after_result = await asyncio.wait_for(
                collecting, timeout=1
            )

        assert (stdout, stderr) == (b"reported result", b"partial stderr")
        assert limit_exceeded is False
        assert stopped_after_result is True
        assert killpg.call_args_list == [
            ((proc.pid, __import__("signal").SIGTERM),),
            ((proc.pid, __import__("signal").SIGKILL),),
        ]

    @pytest.mark.asyncio
    async def test_result_submission_escalates_when_the_agent_ignores_term(self):
        """The completion barrier cannot leave an agent mutating the checkout."""
        wrapper, _ = _make_wrapper()
        proc, _ = self._hanging_process(b"")
        calls: list[int] = []

        with (
            patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
            patch(
                "worker_wrapper.wrapper.os.killpg",
                side_effect=lambda _pid, signal: calls.append(signal),
            ),
        ):
            await wrapper._stop_agent_process_group(proc)

        assert calls == [__import__("signal").SIGTERM, __import__("signal").SIGKILL]

    @pytest.mark.asyncio
    async def test_leader_exit_does_not_leave_a_term_ignoring_descendant_alive(self):
        """A leader exit cannot let a descendant hold stdout or mutate the checkout."""
        wrapper, _ = _make_wrapper()
        wrapper._result_event = asyncio.Event()
        descendant_killed = asyncio.Event()

        async def communicate():
            await descendant_killed.wait()
            return b"leader exited", b""

        proc = MagicMock()
        proc.pid = 4321
        proc.returncode = 0
        proc.communicate = communicate
        proc.wait = AsyncMock(return_value=0)
        signals: list[int] = []

        def kill_group(_pid, sent_signal):
            signals.append(sent_signal)
            if sent_signal == __import__("signal").SIGKILL:
                descendant_killed.set()

        with (
            patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
            patch("worker_wrapper.wrapper.os.killpg", side_effect=kill_group),
        ):
            stdout, stderr, limit_exceeded, stopped_after_result = await asyncio.wait_for(
                wrapper._collect_agent_output(proc), timeout=1
            )

        assert (stdout, stderr, limit_exceeded, stopped_after_result) == (
            b"leader exited",
            b"",
            False,
            False,
        )
        assert signals == [__import__("signal").SIGTERM, __import__("signal").SIGKILL]

    @pytest.mark.asyncio
    async def test_auto_resume_uses_the_completion_barrier(self, tmp_path):
        """A resumed result cannot leave a late-mutating descendant behind."""
        wrapper, broker = _make_wrapper(agent_type="claude", transcript_dir=str(tmp_path))
        broker.get_session.return_value = "resume-session"
        wrapper._result_event = asyncio.Event()
        initial_transcript = tmp_path / "dev-liveness-1" / "resume-1.log"
        initial_transcript.parent.mkdir()
        initial_transcript.write_text("initial transcript", encoding="utf-8")
        wrapper._transcript_path = str(initial_transcript)
        killed = asyncio.Event()

        async def communicate():
            await killed.wait()
            return b"resume result", b""

        async def wait():
            await killed.wait()
            return -9

        proc = MagicMock()
        proc.pid = 4321
        proc.returncode = -9
        proc.communicate = communicate
        proc.wait = wait
        signals: list[int] = []

        def kill_group(_pid, sent_signal):
            signals.append(sent_signal)
            if sent_signal == __import__("signal").SIGKILL:
                killed.set()

        async def fake_exec(*_args, **_kwargs):
            return proc

        with (
            patch("worker_wrapper.wrapper.RESULT_STOP_GRACE_SECONDS", 0),
            patch("worker_wrapper.wrapper.os.killpg", side_effect=kill_group),
            patch(
                "worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec
            ) as create,
        ):
            resumed = asyncio.create_task(wrapper._attempt_auto_resume({"request_id": "resume-1"}))
            await asyncio.sleep(0)
            wrapper._result_event.set()
            assert await asyncio.wait_for(resumed, timeout=1) is True

        assert create.await_args.kwargs["start_new_session"] is True
        assert signals == [__import__("signal").SIGTERM, __import__("signal").SIGKILL]
        transcript = initial_transcript.read_text(encoding="utf-8")
        assert "initial transcript" in transcript
        assert "resume result" in transcript

    @pytest.mark.asyncio
    async def test_normal_exit_awaits_the_cancelled_result_waiter(self):
        """The completion race leaves no pending result waiter behind."""

        class TrackingEvent(asyncio.Event):
            cancelled = False

            async def wait(self):
                try:
                    return await super().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        wrapper, _ = _make_wrapper()
        wrapper._result_event = TrackingEvent()
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"done", b""))
        proc.wait = AsyncMock(return_value=0)

        stdout, stderr, limit_exceeded, stopped_after_result = await wrapper._collect_agent_output(
            proc
        )

        assert (stdout, stderr, limit_exceeded, stopped_after_result) == (
            b"done",
            b"",
            False,
            False,
        )
        assert wrapper._result_event.cancelled is True

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
