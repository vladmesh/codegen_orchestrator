"""Tests for wrapper multi-turn support (Iteration 1: worker-reuse-ci-fix)."""

from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.config import WorkerWrapperConfig
from worker_wrapper.wrapper import WorkerWrapper


@pytest.fixture(autouse=True)
def _no_workspace_check():
    """Skip workspace preflight — these tests run outside containers."""
    with patch("worker_wrapper.wrapper.WORKSPACE_DIR", "/nonexistent/workspace"):
        yield


@pytest.fixture
def config():
    return WorkerWrapperConfig(
        redis_url="redis://localhost:6379",
        agent_type="claude",
        input_stream="worker:dev-1:input",
        output_stream="worker:dev-1:output",
        consumer_group="workers",
        consumer_name="dev-1",
    )


@pytest.fixture
def fake_redis():
    return FakeAsyncRedis()


@pytest.fixture
def mock_redis_client(fake_redis):
    client = MagicMock()
    client.redis = fake_redis
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.ensure_consumer_group = AsyncMock()
    client.publish = AsyncMock()
    client.publish_message = AsyncMock()
    return client


def _make_message(msg_id, data):
    msg = MagicMock()
    msg.message_id = msg_id
    msg.data = data
    return msg


# ---------- 1.1: Multi-turn consume loop ----------


class TestMultiTurnConsumeLoop:
    @pytest.mark.asyncio
    async def test_wrapper_processes_multiple_messages(self, config, mock_redis_client):
        """Wrapper should process 2+ messages sequentially without exiting."""
        msg1 = _make_message("1-0", {"prompt": "Initial task"})
        msg2 = _make_message("2-0", {"prompt": "CI fix task"})

        call_count = 0

        async def mock_consume(**kwargs):
            nonlocal call_count
            yield msg1
            call_count += 1
            yield msg2
            call_count += 1

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.execute_agent = AsyncMock(return_value=None)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._git_pull = AsyncMock()
        wrapper._write_task_md = MagicMock()

        await wrapper.run()

        assert call_count == 2  # noqa: PLR2004
        assert wrapper.execute_agent.call_count == 2  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_wrapper_continues_after_publishing_output(self, config, mock_redis_client):
        """After publishing output for first message, wrapper keeps listening."""
        msg1 = _make_message("1-0", {"prompt": "Task 1"})
        msg2 = _make_message("2-0", {"prompt": "Task 2"})

        outputs = []

        async def track_publish(stream, data):
            outputs.append(data)

        mock_redis_client.publish = AsyncMock(side_effect=track_publish)

        async def mock_consume(**kwargs):
            yield msg1
            yield msg2

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        # execute_agent returns None — no HTTP result → watchdog fires
        wrapper.execute_agent = AsyncMock(return_value=None)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._git_pull = AsyncMock()
        wrapper._write_task_md = MagicMock()

        await wrapper.run()

        # Watchdog publishes failed for each message (no HTTP result)
        assert len(outputs) == 2  # noqa: PLR2004
        assert outputs[0]["status"] == "failed"
        assert outputs[1]["status"] == "failed"


# ---------- 1.2: git pull before each turn ----------


class TestGitPullBeforeTurn:
    @pytest.mark.asyncio
    async def test_git_pull_called_before_execute_agent(self, config, mock_redis_client):
        """_git_pull() must be called before each execute_agent()."""
        msg = _make_message("1-0", {"prompt": "Fix CI"})

        async def mock_consume(**kwargs):
            yield msg

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._write_task_md = MagicMock()

        call_order = []

        async def track_git_pull():
            call_order.append("git_pull")

        async def track_execute(data):
            call_order.append("execute_agent")

        wrapper._git_pull = track_git_pull
        wrapper.execute_agent = track_execute

        await wrapper.run()

        assert call_order == ["git_pull", "execute_agent"]

    @pytest.mark.asyncio
    async def test_git_pull_called_before_each_turn(self, config, mock_redis_client):
        """_git_pull() is called before every turn, not just the first."""
        msg1 = _make_message("1-0", {"prompt": "Task 1"})
        msg2 = _make_message("2-0", {"prompt": "Task 2"})

        async def mock_consume(**kwargs):
            yield msg1
            yield msg2

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._write_task_md = MagicMock()

        call_order = []

        async def track_git_pull():
            call_order.append("git_pull")

        async def track_execute(data):
            call_order.append("execute_agent")

        wrapper._git_pull = track_git_pull
        wrapper.execute_agent = track_execute

        await wrapper.run()

        assert call_order == ["git_pull", "execute_agent", "git_pull", "execute_agent"]

    @pytest.mark.asyncio
    async def test_git_pull_runs_git_command(self, config, mock_redis_client):
        """_git_pull() should run 'git pull --rebase=false' for current branch."""
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", "/workspace"):
            wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

            with (
                patch("subprocess.run") as mock_run,
                patch.object(wrapper, "_get_git_branch", return_value="main"),
            ):
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                await wrapper._git_pull()

                mock_run.assert_called_once_with(
                    ["/usr/bin/git", "pull", "--rebase=false", "origin", "main"],
                    cwd="/workspace",
                    capture_output=True,
                    text=True,
                    timeout=60,
                )

    @pytest.mark.asyncio
    async def test_git_pull_failure_does_not_crash(self, config, mock_redis_client):
        """git pull failure should log warning but not raise."""
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", "/workspace"):
            wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

            with (
                patch("subprocess.run") as mock_run,
                patch.object(wrapper, "_get_git_branch", return_value="main"),
            ):
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="merge conflict")
                # Should not raise
                await wrapper._git_pull()


# ---------- 1.3: Update TASK.md before each turn ----------


class TestTaskMdUpdate:
    @pytest.mark.asyncio
    async def test_task_md_updated_before_execute_agent(self, config, mock_redis_client):
        """TASK.md should be written with prompt before execute_agent."""
        msg = _make_message("1-0", {"prompt": "Fix the CI error in tests"})

        async def mock_consume(**kwargs):
            yield msg

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._git_pull = AsyncMock()

        call_order = []

        def track_write(prompt):
            call_order.append(("write_task_md", prompt))

        async def track_execute(data):
            call_order.append("execute_agent")

        wrapper._write_task_md = track_write
        wrapper.execute_agent = track_execute

        await wrapper.run()

        assert call_order == [
            ("write_task_md", "Fix the CI error in tests"),
            "execute_agent",
        ]

    @pytest.mark.asyncio
    async def test_task_md_updated_each_turn(self, config, mock_redis_client):
        """TASK.md is updated with each new prompt."""
        msg1 = _make_message("1-0", {"prompt": "Initial task"})
        msg2 = _make_message("2-0", {"prompt": "Fix CI failure"})

        async def mock_consume(**kwargs):
            yield msg1
            yield msg2

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._git_pull = AsyncMock()

        prompts_written = []

        def track_write(prompt):
            prompts_written.append(prompt)

        wrapper._write_task_md = track_write
        wrapper.execute_agent = AsyncMock(return_value=None)

        await wrapper.run()

        assert prompts_written == ["Initial task", "Fix CI failure"]

    def test_write_task_md_writes_file(self, config, mock_redis_client, tmp_path):
        """_write_task_md should write prompt content to TASK.md path."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        task_path = tmp_path / "TASK.md"
        with patch("worker_wrapper.wrapper.TASK_MD_PATH", str(task_path)):
            wrapper._write_task_md("Fix the broken tests\n\nCI logs: error in test_foo")

        content = task_path.read_text()
        assert content == "Fix the broken tests\n\nCI logs: error in test_foo"

    @pytest.mark.asyncio
    async def test_no_task_md_update_when_no_prompt(self, config, mock_redis_client):
        """If message has no prompt, _write_task_md should not be called."""
        msg = _make_message("1-0", {"content": "Some PO message"})

        async def mock_consume(**kwargs):
            yield msg

        mock_redis_client.consume = mock_consume

        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)
        wrapper.publish_lifecycle = AsyncMock()
        wrapper._git_pull = AsyncMock()
        wrapper._write_task_md = MagicMock()
        wrapper.execute_agent = AsyncMock(return_value=None)

        await wrapper.run()

        # content-based messages (PO) don't update TASK.md
        wrapper._write_task_md.assert_not_called()


# ---------- 1.4: Task archiving to .story/old_tasks/ ----------


class TestTaskArchiving:
    def test_archive_creates_old_tasks_dir(self, config, mock_redis_client, tmp_path):
        """_archive_task creates .story/old_tasks/ and writes merged file."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        task_md = tmp_path / "TASK.md"
        task_md.write_text("# Task: Build API\n\nImplement GET /users")
        gitignore = tmp_path / ".gitignore"

        with (
            patch("worker_wrapper.wrapper.TASK_MD_PATH", str(task_md)),
            patch("worker_wrapper.wrapper.OLD_TASKS_DIR", str(tmp_path / ".story" / "old_tasks")),
            patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)),
        ):
            wrapper._archive_task(
                {"task_id": "task-abc123", "request_id": "req-1"},
                report="# Developer Report\n\n## Summary\nAll done.",
            )

        archive = tmp_path / ".story" / "old_tasks" / "task-abc123.md"
        assert archive.exists()
        content = archive.read_text()
        assert "# Task: Build API" in content
        assert "# Developer Report" in content
        assert "---" in content  # separator between task and report

        # .gitignore should have .story/ entry
        assert ".story/" in gitignore.read_text()

    def test_archive_without_report(self, config, mock_redis_client, tmp_path):
        """Archive works without a report — just saves task description."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        task_md = tmp_path / "TASK.md"
        task_md.write_text("# Task: Fix bug")

        with (
            patch("worker_wrapper.wrapper.TASK_MD_PATH", str(task_md)),
            patch("worker_wrapper.wrapper.OLD_TASKS_DIR", str(tmp_path / ".story" / "old_tasks")),
            patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)),
        ):
            wrapper._archive_task({"request_id": "req-456"}, report=None)

        archive = tmp_path / ".story" / "old_tasks" / "req-456.md"
        assert archive.exists()
        content = archive.read_text()
        assert "# Task: Fix bug" in content
        assert "---" not in content  # no separator without report

    def test_archive_uses_request_id_fallback(self, config, mock_redis_client, tmp_path):
        """Falls back to request_id when task_id is not in data."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        task_md = tmp_path / "TASK.md"
        task_md.write_text("# Task")

        with (
            patch("worker_wrapper.wrapper.TASK_MD_PATH", str(task_md)),
            patch("worker_wrapper.wrapper.OLD_TASKS_DIR", str(tmp_path / ".story" / "old_tasks")),
            patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)),
        ):
            wrapper._archive_task({"request_id": "fallback-id"}, report=None)

        assert (tmp_path / ".story" / "old_tasks" / "fallback-id.md").exists()

    def test_archive_noop_when_no_task_md(self, config, mock_redis_client, tmp_path):
        """No crash when TASK.md doesn't exist."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        with patch("worker_wrapper.wrapper.TASK_MD_PATH", str(tmp_path / "nonexistent.md")):
            wrapper._archive_task({"task_id": "t-1"}, report="some report")
            # Should not raise

    def test_gitignore_not_duplicated(self, config, mock_redis_client, tmp_path):
        """_ensure_gitignore_entry doesn't add duplicate entries."""
        wrapper = WorkerWrapper(config, redis_client=mock_redis_client)

        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("node_modules/\n.story/\n")

        wrapper._ensure_gitignore_entry(str(gitignore), ".story/")

        lines = gitignore.read_text().splitlines()
        assert lines.count(".story/") == 1
