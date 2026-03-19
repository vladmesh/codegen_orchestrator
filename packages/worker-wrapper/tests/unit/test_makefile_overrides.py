"""Tests for _inject_makefile_overrides()."""

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
        makefile.write_text("dev-start:\n\tdocker compose up -d $(svc)\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        assert "# --- orchestrator overrides ---" in content
        assert "http://localhost:9090/infra/compose" in content
        assert "dev-start:" in content
        assert "dev-stop:" in content

    def test_dev_start_passes_svc_variable(self, wrapper, tmp_path):
        """dev-start override passes $(svc) in the compose args."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("dev-start:\n\tdocker compose up -d $(svc)\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        # The curl payload should include $(svc) for service selection
        assert "$(svc)" in content
        assert '"up", "-d", "--wait"' in content

    def test_dev_stop_override(self, wrapper, tmp_path):
        """dev-stop override sends down --remove-orphans."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("dev-start:\n\tdocker compose up -d\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        assert '"down", "--remove-orphans"' in content

    def test_idempotent_no_duplicate(self, wrapper, tmp_path):
        """Second call does not duplicate the override block."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("dev-start:\n\tdocker compose up -d $(svc)\n")

        wrapper._inject_makefile_overrides()
        content_after_first = makefile.read_text()

        wrapper._inject_makefile_overrides()
        content_after_second = makefile.read_text()

        assert content_after_first == content_after_second

    def test_no_makefile_noop(self, wrapper, tmp_path):
        """No Makefile in workspace → no crash, no file created."""
        wrapper._inject_makefile_overrides()

        assert not (tmp_path / "Makefile").exists()

    def test_override_marker_present(self, wrapper, tmp_path):
        """Override block starts with the marker comment."""
        makefile = tmp_path / "Makefile"
        makefile.write_text("all:\n\techo hello\n")

        wrapper._inject_makefile_overrides()

        content = makefile.read_text()
        assert "# --- orchestrator overrides ---" in content
