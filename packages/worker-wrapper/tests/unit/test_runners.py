import json
import subprocess
from types import SimpleNamespace
import urllib.request

import pytest
from worker_wrapper.runners.claude import ClaudeRunner
from worker_wrapper.runners.codex import CodexRunner
from worker_wrapper.runners.factory import FactoryRunner
from worker_wrapper.runners.noop import NoopRunner


class TestClaudeRunner:
    def test_build_command_includes_headless_flags(self):
        """Command should include required Claude headless flags."""
        runner = ClaudeRunner(session_id="sess-123")
        cmd = runner.build_command(prompt="Create hello.py")

        # Check command parts
        assert "claude" in cmd

        # Should contain flags.
        assert "-p" in cmd
        assert "Create hello.py" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd

        # Session resumption
        assert "--resume" in cmd
        # The session ID should be the argument after --resume or attached
        # We assume build_command returns a list of strings: ["claude", "-p", ...]
        assert "sess-123" in cmd

    def test_build_command_without_session_omits_resume(self):
        """Without session_id, should not include --resume."""
        runner = ClaudeRunner(session_id=None)
        cmd = runner.build_command(prompt="Hello")
        assert "--resume" not in cmd


class TestFactoryRunner:
    def test_build_command_uses_droid_exec(self):
        """Factory should use droid exec command."""
        runner = FactoryRunner()
        cmd = runner.build_command(prompt="Fix bug")

        # Check basic command structure
        cmd_str = " ".join(cmd)
        assert "droid" in cmd_str
        assert "exec" in cmd

        # Check flags
        assert "--skip-permissions-unsafe" in cmd
        assert "--cwd" in cmd
        assert "/workspace" in cmd
        assert "-o" in cmd
        assert "json" in cmd

        # Prompt should be present
        assert "Fix bug" in cmd


class TestCodexRunner:
    def test_build_command_uses_non_interactive_workspace_sandbox(self):
        cmd = CodexRunner().build_command(prompt="Read TASK.md and AGENTS.md")

        assert cmd == [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "Read TASK.md and AGENTS.md",
        ]


class TestNoopRunner:
    def test_pushes_checked_out_branch_and_reports_success_over_http(self):
        command = " ".join(NoopRunner().build_command(prompt="ignored"))

        assert "rev-parse" in command
        assert "--abbrev-ref" in command
        assert 'push", "origin", branch' in command
        assert "push origin main" not in command
        assert "http://127.0.0.1:9090/result" in command
        assert '"success": True' in command

    def test_reports_git_failure_over_http_before_exiting(self):
        command = " ".join(NoopRunner().build_command(prompt="ignored"))

        assert '"success": False' in command
        assert "reason" in command
        assert "returncode" in command

    def test_failure_result_does_not_include_git_output(self):
        command = " ".join(NoopRunner().build_command(prompt="ignored"))

        assert "failed.stderr" not in command
        assert "failed.stdout" not in command
        assert '"exit_code"' in command
        assert '"error_class"' in command

    def test_failure_result_and_logs_redact_git_output(self, monkeypatch, capsys):
        raw_git_output = "https://token-like-value@example.invalid/repo.git"
        captured = {}

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=128,
                stdout=f"clone {raw_git_output}",
                stderr=f"fatal: {raw_git_output}",
            ),
        )

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        def urlopen(request, timeout):
            captured["payload"] = json.loads(request.data)
            return Response()

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        script = NoopRunner().build_command(prompt="ignored")[2]

        with pytest.raises(SystemExit):
            exec(script, {"__name__": "__main__"})  # noqa: S102

        assert raw_git_output not in json.dumps(captured["payload"])
        assert raw_git_output not in capsys.readouterr().out
