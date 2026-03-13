"""Unit tests for the workspace introspection API router."""

from http import HTTPStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers.workspaces import router as workspaces_router


def _make_app(workspace_base="/tmp/ws", scaffolded_base="/data/ws"):
    """Create a test FastAPI app with mocked settings."""
    app = FastAPI()
    app.include_router(workspaces_router)
    # Store base paths for the router to use
    app.state.workspace_base_path = workspace_base
    app.state.scaffolded_workspace_path = scaffolded_base
    return app


class TestGetWorkspaceTree:
    def test_returns_file_tree(self, tmp_path):
        workspace = tmp_path / "proj-1" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('hello')")
        (workspace / "README.md").write_text("# Hello")

        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-1/tree")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        paths = [e["path"] for e in data]
        assert "src" in paths
        assert "src/main.py" in paths
        assert "README.md" in paths

    def test_workspace_not_found_returns_404(self, tmp_path):
        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/nonexistent/tree")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_scaffolded_workspace_fallback(self, tmp_path):
        """Falls back to SCAFFOLDED_WORKSPACE_PATH if no project workspace."""
        scaffolded = tmp_path / "scaffolded" / "proj-2"
        scaffolded.mkdir(parents=True)
        (scaffolded / "app.py").write_text("app = Flask(__name__)")

        app = _make_app(
            workspace_base=str(tmp_path / "workspaces"),
            scaffolded_base=str(tmp_path / "scaffolded"),
        )
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-2/tree")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        paths = [e["path"] for e in data]
        assert "app.py" in paths


class TestGetWorkspaceFile:
    def test_reads_valid_file(self, tmp_path):
        workspace = tmp_path / "proj-1" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "hello.txt").write_text("world")

        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-1/files/hello.txt")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["content"] == "world"
        assert data["project_id"] == "proj-1"

    def test_symlink_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "proj-1" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "escape").symlink_to("/etc")

        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-1/files/escape/hostname")
        assert resp.status_code == HTTPStatus.FORBIDDEN

    def test_file_not_found(self, tmp_path):
        workspace = tmp_path / "proj-1" / "workspace"
        workspace.mkdir(parents=True)

        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-1/files/nope.txt")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_binary_file_rejected(self, tmp_path):
        workspace = tmp_path / "proj-1" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "binary.bin").write_bytes(b"\x00\x01\x02\xff\xfe")

        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-1/files/binary.bin")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_workspace_not_found(self, tmp_path):
        app = _make_app(workspace_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/nonexistent/files/foo.txt")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_scaffolded_fallback_file(self, tmp_path):
        scaffolded = tmp_path / "scaffolded" / "proj-2"
        scaffolded.mkdir(parents=True)
        (scaffolded / "main.py").write_text("print('hi')")

        app = _make_app(
            workspace_base=str(tmp_path / "workspaces"),
            scaffolded_base=str(tmp_path / "scaffolded"),
        )
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/proj-2/files/main.py")
        assert resp.status_code == HTTPStatus.OK
        assert resp.json()["content"] == "print('hi')"
