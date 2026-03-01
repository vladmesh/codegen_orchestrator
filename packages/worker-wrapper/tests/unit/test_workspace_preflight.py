"""Unit tests for workspace pre-flight check in WorkerWrapper."""

from __future__ import annotations

from unittest.mock import patch

from worker_wrapper.wrapper import WorkerWrapper


def _make_wrapper():
    """Create a WorkerWrapper with minimal config (no Redis needed for preflight)."""
    from worker_wrapper.config import WorkerWrapperConfig

    config = WorkerWrapperConfig(
        redis_url="redis://fake:6379",
        input_stream="test:input",
        output_stream="test:output",
        consumer_group="test_group",
        consumer_name="test_worker",
        agent_type="claude",
    )
    # Pass a fake redis to avoid connection
    return WorkerWrapper(config, redis_client=None)


class TestWorkspacePreflight:
    """Tests for _check_workspace_ready()."""

    def test_missing_copier_marker_fails(self, tmp_path):
        """Workspace without .copier-answers.yml → fail."""
        # Create a minimal workspace (like an empty scaffolded repo)
        (tmp_path / "README.md").write_text("# Test")
        (tmp_path / "CLAUDE.md").write_text("instructions")

        wrapper = _make_wrapper()
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)):
            ok, detail = wrapper._check_workspace_ready()

        assert not ok
        assert ".copier-answers.yml" in detail

    def test_scaffolded_workspace_passes(self, tmp_path):
        """Workspace with scaffold markers → pass."""
        (tmp_path / ".copier-answers.yml").write_text("_src_path: gh:test/template")
        (tmp_path / "Makefile").write_text("setup:")
        (tmp_path / "services").mkdir()

        wrapper = _make_wrapper()
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)):
            ok, detail = wrapper._check_workspace_ready()

        assert ok
        assert "copier_marker=True" in detail
        assert "makefile=True" in detail
        assert "services_dir=True" in detail

    def test_nonexistent_workspace_skips(self):
        """Non-existent workspace directory → skip (not in container)."""
        wrapper = _make_wrapper()
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", "/nonexistent/workspace"):
            ok, detail = wrapper._check_workspace_ready()

        assert ok
        assert "not in container" in detail

    def test_copier_marker_only_passes(self, tmp_path):
        """Workspace with just .copier-answers.yml (minimum) → pass."""
        (tmp_path / ".copier-answers.yml").write_text("_src_path: gh:test/template")

        wrapper = _make_wrapper()
        with patch("worker_wrapper.wrapper.WORKSPACE_DIR", str(tmp_path)):
            ok, detail = wrapper._check_workspace_ready()

        assert ok
