from worker_wrapper.runners.claude import ClaudeRunner
from worker_wrapper.runners.factory import FactoryRunner


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
        assert "-o" in cmd
        assert "json" in cmd

        # Prompt should be present
        assert "Fix bug" in cmd
