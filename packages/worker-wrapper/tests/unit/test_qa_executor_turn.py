"""A QA executor's turn, and how it differs from a developer's.

The wrapper is the same process doing the same job — lease a task, run the CLI,
publish — but a QA executor has no repository, so every repository-shaped step
of a turn has to be absent rather than merely tolerated. A `git pull` in an
empty scratch directory, a scaffold pre-flight that refuses an empty workspace,
or an auto-resume chasing a result that travels on another channel would each
end the run before any testing happened.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from worker_wrapper.broker import BrokerMessage
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper, build_agent_subprocess_env


def _config(**overrides) -> WorkerWrapperConfig:
    defaults = {
        "broker_url": "http://worker-broker:8001",
        "broker_token": "x" * 43,
        "worker_id": "qa-1",
        "agent_type": "claude",
        "worker_type": "qa",
    }
    defaults.update(overrides)
    return WorkerWrapperConfig(**defaults)


def _wrapper(**overrides) -> WorkerWrapper:
    broker = MagicMock()
    broker.update_status = AsyncMock()
    broker.submit_output = AsyncMock()
    broker.get_session = AsyncMock(return_value=None)
    broker.set_session = AsyncMock()
    broker.clear_session = AsyncMock()
    broker.compose = AsyncMock()
    return WorkerWrapper(config=_config(**overrides), broker_client=broker)


class TestWhatAQaTurnSkips:
    def test_the_worker_type_is_read_from_the_container_environment(self):
        """worker-manager sets WORKER_TYPE; the wrapper must actually read it."""
        with patch.dict(os.environ, {"WORKER_TYPE": "qa"}, clear=False):
            config = WorkerWrapperConfig(
                broker_url="http://worker-broker:8001",
                broker_token="x" * 43,
                worker_id="qa-1",
                agent_type="claude",
            )

        assert config.worker_type == "qa"

    def test_a_developer_worker_is_still_the_default(self):
        config = WorkerWrapperConfig(
            broker_url="http://worker-broker:8001",
            broker_token="x" * 43,
            worker_id="dev-1",
            agent_type="claude",
        )

        assert config.worker_type == "developer"
        assert WorkerWrapper(config=config, broker_client=MagicMock()).is_qa_executor is False

    async def test_no_git_pull_happens_in_a_scratch_workspace(self):
        wrapper = _wrapper()

        with (
            patch.object(wrapper, "_git_pull", new_callable=AsyncMock) as git_pull,
            patch.object(wrapper, "_write_task_md"),
        ):
            await wrapper._prepare_workspace({"prompt": "run the regression test"})

        git_pull.assert_not_awaited()

    async def test_an_empty_workspace_does_not_refuse_the_run(self, tmp_path):
        """The scaffold pre-flight is a developer check and would refuse every QA run."""
        wrapper = _wrapper()

        with (
            patch.object(wrapper, "_prepare_workspace", new_callable=AsyncMock),
            patch.object(wrapper, "_check_workspace_ready") as preflight,
            patch.object(wrapper, "_fix_venv_paths") as venv,
            patch.object(wrapper, "_inject_makefile_overrides") as makefile,
            patch.object(wrapper, "execute_agent", new_callable=AsyncMock),
            patch.object(wrapper, "_read_worker_report", return_value=None),
            patch.object(wrapper, "_archive_task") as archive,
            patch.object(wrapper, "_publish_result", new_callable=AsyncMock),
        ):
            await wrapper.process_message("msg-1", {"prompt": "test it"})

        preflight.assert_not_called()
        venv.assert_not_called()
        makefile.assert_not_called()
        archive.assert_not_called()

    async def test_a_developer_turn_still_runs_every_one_of_them(self):
        wrapper = _wrapper(worker_type="developer", worker_id="dev-1")

        with (
            patch.object(wrapper, "_prepare_workspace", new_callable=AsyncMock),
            patch.object(wrapper, "_check_workspace_ready", return_value=(True, "ok")) as preflight,
            patch.object(wrapper, "_fix_venv_paths") as venv,
            patch.object(wrapper, "_inject_makefile_overrides") as makefile,
            patch.object(wrapper, "execute_agent", new_callable=AsyncMock),
            patch.object(wrapper, "_read_worker_report", return_value=None),
            patch.object(wrapper, "_archive_task") as archive,
            patch.object(wrapper, "_publish_result", new_callable=AsyncMock),
        ):
            await wrapper.process_message("msg-1", {"prompt": "write code"})

        preflight.assert_called_once()
        venv.assert_called_once()
        makefile.assert_called_once()
        archive.assert_called_once()

    async def test_a_qa_executor_is_not_auto_resumed(self):
        """Its verdict never travels on this channel, so resuming chases nothing."""
        wrapper = _wrapper()
        wrapper._result_event = MagicMock()
        wrapper._result_event.is_set.return_value = False
        wrapper._buffered_result = None

        with patch.object(wrapper, "_attempt_auto_resume", new_callable=AsyncMock) as resume:
            await wrapper._publish_result("msg-1", {}, None, "completed", None)

        resume.assert_not_awaited()

    async def test_an_immediate_turn_waits_for_every_manager_injected_qa_material(self, tmp_path):
        """The container may start first, but must not lease before preparation finishes."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        wrapper = _wrapper(agent_type="codex")
        wrapper.config.poll_interval_ms = 1
        leased = asyncio.Event()
        verdict_submitted = asyncio.Event()
        submitted_verdict: dict[str, object] | None = None
        turns = 0

        async def lease_input():
            nonlocal turns
            leased.set()
            turns += 1
            if turns == 1:
                return BrokerMessage(
                    message_id="first-turn", data={"prompt": "test the deployment"}
                )
            wrapper._running = False
            return None

        wrapper.broker.lease_input = AsyncMock(side_effect=lease_input)

        async def execute_agent(_data):
            nonlocal submitted_verdict
            assert (workspace / "AGENTS.md").read_text() == "# QA executor"
            assert (workspace / "TASK.md").read_text() == "test the deployment"
            assert (workspace / "qa").is_file()
            submitted_verdict = {"pass": True, "checks": [], "summary": "ready"}
            verdict_submitted.set()

        with (
            patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(workspace)),
            patch("worker_wrapper.wrapper.TASK_MD_PATH", str(workspace / "TASK.md")),
            patch.object(wrapper, "execute_agent", side_effect=execute_agent),
            patch.object(wrapper, "_prepare_workspace", new_callable=AsyncMock),
            patch.object(wrapper, "_publish_result", new_callable=AsyncMock),
        ):
            run = asyncio.create_task(wrapper.run())
            await asyncio.sleep(0)
            assert not leased.is_set()

            (workspace / "AGENTS.md").write_text("# QA executor")
            await asyncio.sleep(0)
            assert not leased.is_set()

            (workspace / "TASK.md").write_text("test the deployment")
            (workspace / "qa").write_text("#!/bin/sh\n")
            await asyncio.sleep(0)
            assert not leased.is_set()
            (workspace / "qa").chmod(0o700)
            await asyncio.wait_for(verdict_submitted.wait(), timeout=1)
            await run

        assert wrapper.broker.lease_input.await_count == 2
        assert submitted_verdict == {"pass": True, "checks": [], "summary": "ready"}


class TestWhatAQaExecutorIsGiven:
    def test_the_capability_endpoint_reaches_the_agent(self):
        env = build_agent_subprocess_env(
            {
                "PATH": "/usr/bin",
                "QA_CAPABILITY_URL": "http://qa-worker:41234/qa/call",
                "QA_CAPABILITY_TOKEN": "run-token",
            }
        )

        assert env["QA_CAPABILITY_URL"] == "http://qa-worker:41234/qa/call"
        assert env["QA_CAPABILITY_TOKEN"] == "run-token"  # noqa: S105 — a test fixture

    def test_the_workspace_command_is_callable_by_name(self):
        env = build_agent_subprocess_env({"PATH": "/usr/bin"}, qa_executor=True)

        assert env["PATH"].split(os.pathsep)[0] == "/workspace"

    def test_a_developer_agent_does_not_get_the_workspace_on_path(self):
        env = build_agent_subprocess_env({"PATH": "/usr/bin"})

        assert env["PATH"] == "/usr/bin"

    @pytest.mark.parametrize(
        "leaked",
        ["WORKER_BROKER_TOKEN", "WORKER_BROKER_URL", "SECRETS_ENCRYPTION_KEY", "REDIS_URL"],
    )
    def test_the_wrappers_own_transport_still_stays_with_the_wrapper(self, leaked):
        env = build_agent_subprocess_env({"PATH": "/usr/bin", leaked: "secret"}, qa_executor=True)

        assert leaked not in env


# What worker-manager puts in the QA executor's container: the capability
# endpoint of the run, the wrapper's own transport, and the run's egress proxy.
# Spelled out here because the defect this class exists for was invisible at the
# container boundary — the container had all of it, and the agent child did not.
_QA_CONTAINER_ENV = {
    "PATH": "/usr/bin",
    "HOME": "/home/worker",
    "CLAUDE_CONFIG_DIR": "/home/worker/.claude",
    "QA_CAPABILITY_URL": "http://qa-worker:41234/qa/call",
    "QA_CAPABILITY_TOKEN": "run-token",
    "HTTPS_PROXY": "http://qa-egress-qa-1:3128",
    "https_proxy": "http://qa-egress-qa-1:3128",
    "NO_PROXY": "localhost,127.0.0.1,qa-worker,worker-broker",
    "no_proxy": "localhost,127.0.0.1,qa-worker,worker-broker",
    "WORKER_BROKER_TOKEN": "wrapper-transport-token",
    "WORKER_BROKER_URL": "http://worker-broker:8001",
}


async def _agent_child_env(wrapper: WorkerWrapper, container_env: dict[str, str]) -> dict[str, str]:
    """The environment `create_subprocess_exec` is actually called with.

    The assertion has to be made here and not on `build_agent_subprocess_env`,
    and not on the container's own environment: the run is decided by what the
    CLI process inherits, and that is this dict and nothing else.
    """
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        proc.kill = AsyncMock()
        proc.wait = AsyncMock()
        return proc

    with (
        patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch.object(wrapper, "_resolve_prompt", return_value="test the deployment"),
        patch.dict(os.environ, container_env, clear=True),
    ):
        await wrapper.execute_agent({"prompt": "test the deployment"})

    assert "env" in captured, "execute_agent must pass env= to create_subprocess_exec"
    return captured["env"]


class TestTheQaAgentChildProcessCanReachItsBackend:
    """The QA executor's CLI is a child process, and that is where a run dies.

    A QA container sits on exactly one internal network. Its only way out is the
    run's CONNECT proxy, and the only thing that tells the CLI the proxy exists
    is these four variables. Dropping them on the way to the child turns a
    working subscription into `qa_executor_unavailable` — the executor never
    reaches its model backend, and with the intended empty `QA_LLM_*` there is
    nothing to fall back to. The container's environment proves nothing about
    this; the child's does.
    """

    async def test_the_run_s_egress_proxy_reaches_the_cli(self):
        env = await _agent_child_env(_wrapper(), _QA_CONTAINER_ENV)

        assert env["HTTPS_PROXY"] == "http://qa-egress-qa-1:3128"
        assert env["https_proxy"] == "http://qa-egress-qa-1:3128"
        assert env["NO_PROXY"] == "localhost,127.0.0.1,qa-worker,worker-broker"
        assert env["no_proxy"] == "localhost,127.0.0.1,qa-worker,worker-broker"

    async def test_the_proxy_arrives_without_the_wrappers_transport(self):
        """The egress variables are added; nothing else is."""
        env = await _agent_child_env(_wrapper(), _QA_CONTAINER_ENV)

        assert "WORKER_BROKER_TOKEN" not in env
        assert "WORKER_BROKER_URL" not in env
        assert env["QA_CAPABILITY_URL"] == "http://qa-worker:41234/qa/call"
        assert env["PATH"].split(os.pathsep)[0] == "/workspace"

    async def test_a_developer_agent_is_not_given_a_proxy_it_was_never_meant_to_use(self):
        """The pass-through is the QA executor's, not every agent's."""
        env = await _agent_child_env(
            _wrapper(worker_type="developer", worker_id="dev-1"), _QA_CONTAINER_ENV
        )

        for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
            assert name not in env

    async def test_a_codex_qa_turn_uses_the_non_git_workspace_mode(self):
        wrapper = _wrapper(agent_type="codex")
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["cmd"] = args
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with (
            patch("worker_wrapper.wrapper.asyncio.create_subprocess_exec", side_effect=fake_exec),
            patch.dict(os.environ, _QA_CONTAINER_ENV, clear=True),
        ):
            await wrapper.execute_agent(
                {"prompt": "Read AGENTS.md and TASK.md, then submit a verdict."}
            )

        assert "--skip-git-repo-check" in captured["cmd"]
