"""Unit tests for the workspace introspection API router."""

from http import HTTPStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routers.workspaces import router as workspaces_router


def _make_app(scaffolded_base="/data/ws"):
    """Create a test FastAPI app with mocked settings."""
    app = FastAPI()
    app.include_router(workspaces_router)
    app.state.scaffolded_workspace_path = scaffolded_base
    return app


class TestGetWorkspaceTree:
    def test_returns_file_tree(self, tmp_path):
        workspace = tmp_path / "repo-1"
        workspace.mkdir(parents=True)
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('hello')")
        (workspace / "README.md").write_text("# Hello")

        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/repo-1/tree")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        paths = [e["path"] for e in data]
        assert "src" in paths
        assert "src/main.py" in paths
        assert "README.md" in paths

    def test_workspace_not_found_returns_404(self, tmp_path):
        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/nonexistent/tree")
        assert resp.status_code == HTTPStatus.NOT_FOUND


class TestGetWorkspaceFile:
    def test_reads_valid_file(self, tmp_path):
        workspace = tmp_path / "repo-1"
        workspace.mkdir(parents=True)
        (workspace / "hello.txt").write_text("world")

        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/repo-1/files/hello.txt")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["content"] == "world"
        assert data["repo_id"] == "repo-1"

    def test_symlink_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "repo-1"
        workspace.mkdir(parents=True)
        (workspace / "escape").symlink_to("/etc")

        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/repo-1/files/escape/hostname")
        assert resp.status_code == HTTPStatus.FORBIDDEN

    def test_file_not_found(self, tmp_path):
        workspace = tmp_path / "repo-1"
        workspace.mkdir(parents=True)

        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/repo-1/files/nope.txt")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_binary_file_rejected(self, tmp_path):
        workspace = tmp_path / "repo-1"
        workspace.mkdir(parents=True)
        (workspace / "binary.bin").write_bytes(b"\x00\x01\x02\xff\xfe")

        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/repo-1/files/binary.bin")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_workspace_not_found(self, tmp_path):
        app = _make_app(scaffolded_base=str(tmp_path))
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workspaces/nonexistent/files/foo.txt")
        assert resp.status_code == HTTPStatus.NOT_FOUND
