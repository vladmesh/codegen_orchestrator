"""A QA executor's turn, and how it differs from a developer's.

The wrapper is the same process doing the same job — lease a task, run the CLI,
publish — but a QA executor has no repository, so every repository-shaped step
of a turn has to be absent rather than merely tolerated. A `git pull` in an
empty scratch directory, a scaffold pre-flight that refuses an empty workspace,
or an auto-resume chasing a result that travels on another channel would each
end the run before any testing happened.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
        env = build_agent_subprocess_env({"PATH": "/usr/bin"}, workspace_on_path=True)

        assert env["PATH"].split(os.pathsep)[0] == "/workspace"

    def test_a_developer_agent_does_not_get_the_workspace_on_path(self):
        env = build_agent_subprocess_env({"PATH": "/usr/bin"})

        assert env["PATH"] == "/usr/bin"

    @pytest.mark.parametrize(
        "leaked",
        ["WORKER_BROKER_TOKEN", "WORKER_BROKER_URL", "SECRETS_ENCRYPTION_KEY", "REDIS_URL"],
    )
    def test_the_wrappers_own_transport_still_stays_with_the_wrapper(self, leaked):
        env = build_agent_subprocess_env(
            {"PATH": "/usr/bin", leaked: "secret"}, workspace_on_path=True
        )

        assert leaked not in env
