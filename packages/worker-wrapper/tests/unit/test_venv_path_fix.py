"""Tests for _fix_venv_paths() — shebang, .pth, and direct_url.json rewriting."""

import json

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


def _make_venv(
    tmp_path, service="services/backend", scaffold_prefix="/data/workspaces/repo-abc123/"
):
    """Create a fake venv structure with shebang, .pth, and direct_url.json."""
    svc_dir = tmp_path / service
    venv_bin = svc_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    # Shebang script
    script = venv_bin / "ruff"
    script.write_text(f"#!{scaffold_prefix}{service}/.venv/bin/python\nimport ruff\n")

    # .pth file for shared
    pth = site_packages / "_shared.pth"
    pth.write_text(f"{scaffold_prefix}shared")

    # direct_url.json for shared
    dist_info = site_packages / "shared-0.1.0.dist-info"
    dist_info.mkdir()
    url_json = dist_info / "direct_url.json"
    url_json.write_text(
        json.dumps(
            {
                "url": f"file://{scaffold_prefix}shared",
                "dir_info": {"editable": True},
            }
        )
    )

    # Also create the shared dir so _detect_scaffold_prefix_from_pth can find it
    (tmp_path / "shared").mkdir(exist_ok=True)

    return svc_dir


class TestFixVenvPaths:
    def test_fixes_shebangs(self, wrapper, tmp_path):
        """Shebang lines are rewritten to /workspace prefix."""
        _make_venv(tmp_path)
        wrapper._fix_venv_paths()

        script = tmp_path / "services/backend/.venv/bin/ruff"
        assert script.read_text().startswith(f"#!{tmp_path}/")

    def test_fixes_pth_files(self, wrapper, tmp_path):
        """_shared.pth is rewritten to point to /workspace/shared."""
        _make_venv(tmp_path)
        wrapper._fix_venv_paths()

        pth = tmp_path / "services/backend/.venv/lib/python3.12/site-packages/_shared.pth"
        assert pth.read_text() == f"{tmp_path}/shared"

    def test_fixes_direct_url_json(self, wrapper, tmp_path):
        """direct_url.json url field is rewritten."""
        _make_venv(tmp_path)
        wrapper._fix_venv_paths()

        dist_info = "services/backend/.venv/lib/python3.12/site-packages"
        url_file = tmp_path / dist_info / "shared-0.1.0.dist-info/direct_url.json"
        data = json.loads(url_file.read_text())
        assert data["url"] == f"file://{tmp_path}/shared"

    def test_idempotent_via_sentinel(self, wrapper, tmp_path):
        """Second call is a no-op (sentinel file prevents re-run)."""
        _make_venv(tmp_path)
        wrapper._fix_venv_paths()

        # Corrupt the .pth to verify it's NOT re-processed
        pth = tmp_path / "services/backend/.venv/lib/python3.12/site-packages/_shared.pth"
        pth.write_text("/some/other/path")

        wrapper._fix_venv_paths()
        assert pth.read_text() == "/some/other/path"  # not touched again

    def test_sentinel_file_created(self, wrapper, tmp_path):
        """Sentinel .venv_paths_fixed is created after run."""
        _make_venv(tmp_path)
        wrapper._fix_venv_paths()
        assert (tmp_path / ".venv_paths_fixed").exists()

    def test_old_sentinel_removed(self, wrapper, tmp_path):
        """Old .shebangs_fixed sentinel is cleaned up."""
        _make_venv(tmp_path)
        old_sentinel = tmp_path / ".shebangs_fixed"
        old_sentinel.touch()

        wrapper._fix_venv_paths()
        assert not old_sentinel.exists()
        assert (tmp_path / ".venv_paths_fixed").exists()

    def test_no_venv_noop(self, wrapper, tmp_path):
        """No venv in workspace → no crash, sentinel created."""
        wrapper._fix_venv_paths()
        assert (tmp_path / ".venv_paths_fixed").exists()

    def test_already_correct_noop(self, wrapper, tmp_path):
        """Paths already pointing to workspace dir → no changes."""
        svc_dir = tmp_path / "services/backend"
        venv_bin = svc_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        # Already correct shebang
        script = venv_bin / "ruff"
        script.write_text(f"#!{tmp_path}/services/backend/.venv/bin/python\nimport ruff\n")

        # Already correct .pth
        pth = site_packages / "_shared.pth"
        pth.write_text(f"{tmp_path}/shared")

        wrapper._fix_venv_paths()
        assert (tmp_path / ".venv_paths_fixed").exists()
        # Content unchanged
        assert pth.read_text() == f"{tmp_path}/shared"

    def test_multiple_venvs(self, wrapper, tmp_path):
        """Fixes paths in multiple service venvs (backend + tg_bot)."""
        _make_venv(tmp_path, service="services/backend")
        _make_venv(tmp_path, service="services/tg_bot")

        wrapper._fix_venv_paths()

        for svc in ["services/backend", "services/tg_bot"]:
            pth = tmp_path / svc / ".venv/lib/python3.12/site-packages/_shared.pth"
            assert pth.read_text() == f"{tmp_path}/shared"

    def test_root_venv_framework(self, wrapper, tmp_path):
        """Root .venv (framework editable install) is also fixed."""
        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        site_packages = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        scaffold_prefix = "/data/workspaces/repo-abc123/"

        # Shebang
        script = venv_bin / "ruff"
        script.write_text(f"#!{scaffold_prefix}.venv/bin/python\nimport ruff\n")

        # .pth for framework
        pth = site_packages / "_framework.pth"
        pth.write_text(f"{scaffold_prefix}.framework")

        # Create .framework dir so detection works
        (tmp_path / ".framework").mkdir()

        wrapper._fix_venv_paths()

        assert pth.read_text() == f"{tmp_path}/.framework"

    def test_virtualenv_pth_ignored(self, wrapper, tmp_path):
        """_virtualenv.pth (contains Python code, not paths) is not touched."""
        # _make_venv creates the full structure including the _virtualenv.pth test target
        _make_venv(tmp_path)

        svc_dir = tmp_path / "services/backend"
        site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"

        virtualenv_pth = site_packages / "_virtualenv.pth"
        original = "import _virtualenv"
        virtualenv_pth.write_text(original)

        wrapper._fix_venv_paths()
        assert virtualenv_pth.read_text() == original


class TestDetectScaffoldPrefixFromPth:
    def test_detects_from_pth(self, wrapper, tmp_path):
        """Can detect scaffold prefix from .pth when shebangs are already fixed."""
        svc_dir = tmp_path / "services/backend"
        venv_bin = svc_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        # Shebang already correct (so _detect_scaffold_prefix returns None)
        script = venv_bin / "ruff"
        script.write_text(f"#!{tmp_path}/services/backend/.venv/bin/python\n")

        # But .pth still has old path
        pth = site_packages / "_shared.pth"
        pth.write_text("/data/workspaces/repo-abc123/shared")

        # Create the shared dir at workspace root so suffix matching works
        (tmp_path / "shared").mkdir()

        result = wrapper._detect_scaffold_prefix_from_pth()
        assert result == "/data/workspaces/repo-abc123/"

    def test_skips_virtualenv_pth(self, wrapper, tmp_path):
        """Ignores _virtualenv.pth (contains import statement, not a path)."""
        svc_dir = tmp_path / "services/backend"
        site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        pth = site_packages / "_virtualenv.pth"
        pth.write_text("import _virtualenv")

        result = wrapper._detect_scaffold_prefix_from_pth()
        assert result is None

    def test_returns_none_when_already_correct(self, wrapper, tmp_path):
        """Returns None when .pth already points to workspace dir."""
        svc_dir = tmp_path / "services/backend"
        site_packages = svc_dir / ".venv" / "lib" / "python3.12" / "site-packages"
        site_packages.mkdir(parents=True)

        pth = site_packages / "_shared.pth"
        pth.write_text(f"{tmp_path}/shared")

        result = wrapper._detect_scaffold_prefix_from_pth()
        assert result is None
