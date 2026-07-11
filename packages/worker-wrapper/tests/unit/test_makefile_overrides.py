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
