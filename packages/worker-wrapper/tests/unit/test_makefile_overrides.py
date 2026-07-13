"""Tests for _inject_makefile_overrides()."""

import os
import subprocess

import pytest
from worker_wrapper.wrapper import WorkerWrapper


@pytest.fixture
def wrapper(tmp_path, monkeypatch):
    """Create a WorkerWrapper with mocked config and workspace dir."""
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path))

    config = type(
        "Config",
        (),
        {
            "redis_url": "redis://fake",
            "input_stream": "test:in",
            "output_stream": "test:out",
            "consumer_group": "grp",
            "consumer_name": "worker-42",
            "agent_type": "claude",
            "poll_interval_ms": 500,
            "subprocess_timeout_seconds": 300,
            "http_server_port": 9090,
            "model_dump": lambda self: {},
        },
    )()

    from unittest.mock import MagicMock

    redis_mock = MagicMock()
    w = WorkerWrapper.__new__(WorkerWrapper)
    w.config = config
    w.redis = redis_mock
    w._owns_redis = False
    return w


class TestInjectMakefileOverrides:
    def test_override_injected_with_correct_curl(self, wrapper, tmp_path):
        """Override targets use curl to localhost:9090/infra/compose."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("worker-start:\n\tdocker compose up -d --build --wait $(svc)\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        assert "# --- orchestrator overrides ---" in content
        assert "http://localhost:9090/infra/compose" in content
        assert "worker-start:" in content
        assert "worker-stop:" in content

    def test_worker_start_passes_svc_variable_and_build_flags(self, wrapper, tmp_path):
        """worker-start preserves service selection and the template's start flags."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("worker-start:\n\tdocker compose up -d --build --wait $(svc)\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        override = content.split("# --- orchestrator overrides ---", 1)[1]
        assert '"args": ["up", "-d", "--build", "--wait", "$(svc)"]' in override
        assert '"cwd": "."' in override

    def test_worker_stop_override_is_project_scoped(self, wrapper, tmp_path):
        """worker-stop only sends project-scoped down without volume cleanup."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("worker-start:\n\tdocker compose up -d --build --wait\n")

        wrapper._inject_makefile_overrides()

        override = makefile.read_text().split("# --- orchestrator overrides ---", 1)[1]
        assert '"args": ["down", "--remove-orphans"]' in override
        assert "--volumes" not in override
        assert "network" not in override

    def test_local_mode_targets_are_not_overridden(self, wrapper, tmp_path):
        """dev targets keep their template recipes and are not worker-mode aliases."""
        makefile = tmp_path / "Makefile"
        original = "dev-start:\n\tdocker compose -f infra/compose.local.yml up -d\n"
        makefile.write_text(original)

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        override = content.split("# --- orchestrator overrides ---", 1)[1]
        assert original in content
        assert "dev-start:" not in override
        assert "dev-stop:" not in override
        assert "docker" not in override

    def test_idempotent_no_duplicate(self, wrapper, tmp_path):
        """Second call does not duplicate the override block."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("worker-start:\n\tdocker compose up -d --build --wait $(svc)\n")

        wrapper._inject_makefile_overrides()
        content_after_first = makefile.read_text()

        wrapper._inject_makefile_overrides()
        content_after_second = makefile.read_text()

        assert content_after_first == content_after_second

    def test_missing_makefile_fails_workspace_preparation(self, wrapper, tmp_path):
        """A worker without a Makefile cannot safely run worker-mode targets."""
        with pytest.raises(RuntimeError, match="Makefile is missing"):
            wrapper._inject_makefile_overrides()

        assert not (tmp_path / "Makefile").exists()

    def test_override_marker_present(self, wrapper, tmp_path):
        """Override block starts with the marker comment."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("all:\n\techo hello\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        assert "# --- orchestrator overrides ---" in content

    @pytest.mark.parametrize(
        ("curl_exit", "body", "should_succeed", "stderr_fragment"),
        [
            (0, '{"exit_code": 0, "stderr": "safe proxy output"}', True, "safe proxy output"),
            (22, "", False, ""),
            (0, '{"exit_code": 7, "stderr": "compose failed"}', False, "compose failed"),
            (0, "not json", False, "parse error"),
            (0, '{"stderr": "missing exit code"}', False, "missing exit code"),
        ],
    )
    def test_generated_worker_recipe_preserves_runtime_failures(
        self, wrapper, tmp_path, curl_exit, body, should_succeed, stderr_fragment
    ):
        """Generated targets execute proxy failures instead of merely containing safe text."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("all:\n\t@true\n")
        wrapper._inject_makefile_overrides()

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        curl.write_text("#!/bin/sh\nprintf '%s' \"$FAKE_CURL_BODY\"\nexit \"$FAKE_CURL_EXIT\"\n")
        curl.chmod(0o755)
        env = os.environ | {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_CURL_BODY": body,
            "FAKE_CURL_EXIT": str(curl_exit),
        }

        result = subprocess.run(
            ["make", "-s", "worker-start"], cwd=tmp_path, env=env, capture_output=True, text=True
        )

        assert (result.returncode == 0) is should_succeed
        assert stderr_fragment in result.stderr
        if not should_succeed:
            assert "compose proxy failed" not in result.stdout
