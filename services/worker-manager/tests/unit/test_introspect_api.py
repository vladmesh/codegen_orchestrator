"""Unit tests for the introspection API router."""

import pytest
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.routers.introspect import router as introspect_router, _safe_resolve


def _make_app(redis=None, docker=None, worker_manager=None, workspace_base="/tmp/ws"):
    """Create a test FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.include_router(introspect_router)
    app.state.redis = redis or AsyncMock()
    app.state.docker = docker or MagicMock()
    app.state.worker_manager = worker_manager or MagicMock()
    return app


@pytest.fixture
def redis():
    r = AsyncMock()
    r.keys = AsyncMock(return_value=[])
    r.hgetall = AsyncMock(return_value={})
    r.get = AsyncMock(return_value=None)
    return r


@pytest.fixture
def docker():
    d = MagicMock()
    d.get_container_logs = AsyncMock(return_value="log line 1\nlog line 2")
    d.inspect_container = AsyncMock(
        return_value={
            "Id": "abc123",
            "Config": {"Image": "worker:latest"},
        }
    )
    return d


@pytest.fixture
def manager():
    m = MagicMock()
    m.delete_worker = AsyncMock()
    return m


class TestListWorkers:
    def test_empty_list(self, redis):
        redis.keys = AsyncMock(return_value=[])
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/")
        assert resp.status_code == HTTPStatus.OK
        assert resp.json() == []

    def test_returns_workers_with_metadata(self, redis, docker):
        redis.keys = AsyncMock(return_value=["worker:status:w1", "worker:status:w2"])
        redis.hgetall = AsyncMock(
            side_effect=[
                # w1 status
                {"status": "RUNNING"},
                # w1 meta
                {"workspace_path": "/tmp/ws/w1/workspace", "dev_network": "dev_proj_w1", "project_id": "p1"},
                # w2 status
                {"status": "PAUSED"},
                # w2 meta
                {"workspace_path": "/tmp/ws/w2/workspace", "dev_network": "dev_proj_w2"},
            ]
        )
        redis.get = AsyncMock(
            side_effect=[
                # w1 last_activity
                "1710000000",
                # w1 error
                None,
                # w2 last_activity
                None,
                # w2 error
                "something broke",
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == "w1"
        assert data[0]["status"] == "RUNNING"
        assert data[0]["project_id"] == "p1"
        assert data[1]["id"] == "w2"
        assert data[1]["error"] == "something broke"


class TestGetWorker:
    def test_worker_found(self, redis, docker):
        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},  # status
                {"workspace_path": "/tmp/ws/w1/workspace", "dev_network": "net1", "project_id": "p1"},  # meta
            ]
        )
        redis.get = AsyncMock(return_value=None)
        app = _make_app(redis=redis, docker=docker)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["id"] == "w1"
        assert data["container_id"] == "abc123"
        assert data["image"] == "worker:latest"

    def test_worker_not_found(self, redis):
        redis.hgetall = AsyncMock(return_value={})
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/nonexistent")
        assert resp.status_code == HTTPStatus.NOT_FOUND


class TestGetWorkerLogs:
    def test_returns_logs(self, redis, docker):
        redis.hgetall = AsyncMock(return_value={"status": "RUNNING"})
        app = _make_app(redis=redis, docker=docker)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/logs?tail=50")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["worker_id"] == "w1"
        assert "log line" in data["logs"]

    def test_worker_not_found(self, redis):
        redis.hgetall = AsyncMock(return_value={})
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/logs")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_tail_over_max_rejected(self, redis, docker):
        redis.hgetall = AsyncMock(return_value={"status": "RUNNING"})
        app = _make_app(redis=redis, docker=docker)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/logs?tail=99999")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestGetWorkerTree:
    def test_returns_file_tree(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('hello')")
        (workspace / "README.md").write_text("# Hello")

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/tree")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        paths = [e["path"] for e in data]
        assert "src" in paths
        assert "src/main.py" in paths
        assert "README.md" in paths

    def test_workspace_not_found(self, redis):
        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": "/nonexistent/path"},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/tree")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_worker_not_found(self, redis):
        redis.hgetall = AsyncMock(return_value={})
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/tree")
        assert resp.status_code == HTTPStatus.NOT_FOUND


class TestGetWorkerFile:
    def test_reads_valid_file(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("world")

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/files/hello.txt")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["content"] == "world"

    def test_path_traversal_via_symlink_blocked(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Create a symlink that points outside workspace
        (workspace / "escape").symlink_to("/etc")

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/files/escape/hostname")
        assert resp.status_code == HTTPStatus.FORBIDDEN

    def test_file_not_found(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/files/nope.txt")
        assert resp.status_code == HTTPStatus.NOT_FOUND

    def test_binary_file_rejected(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "binary.bin").write_bytes(b"\x00\x01\x02\xff\xfe")

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/files/binary.bin")
        assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


class TestGetWorkerPrompts:
    def test_both_files_exist(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "CLAUDE.md").write_text("# Agent instructions")
        (workspace / "TASK.md").write_text("# Task details")

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/prompts")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert "Agent instructions" in data["claude_md"]
        assert "Task details" in data["task_md"]

    def test_missing_files_return_null(self, redis, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        redis.hgetall = AsyncMock(
            side_effect=[
                {"status": "RUNNING"},
                {"workspace_path": str(workspace)},
            ]
        )
        app = _make_app(redis=redis)
        with TestClient(app) as c:
            resp = c.get("/api/introspect/workers/w1/prompts")
        assert resp.status_code == HTTPStatus.OK
        data = resp.json()
        assert data["claude_md"] is None
        assert data["task_md"] is None


class TestSafeResolve:
    def test_valid_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file.txt").write_text("ok")
        result = _safe_resolve(workspace, "file.txt")
        assert result == (workspace / "file.txt").resolve()

    def test_dotdot_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(workspace, "../../etc/passwd")
        assert exc_info.value.status_code == HTTPStatus.FORBIDDEN

    def test_symlink_traversal_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "link").symlink_to("/etc")
        with pytest.raises(HTTPException) as exc_info:
            _safe_resolve(workspace, "link/hostname")
        assert exc_info.value.status_code == HTTPStatus.FORBIDDEN


class TestDeleteWorker:
    def test_kills_worker(self, redis, manager):
        redis.hgetall = AsyncMock(return_value={"status": "RUNNING"})
        app = _make_app(redis=redis, worker_manager=manager)
        with TestClient(app) as c:
            resp = c.delete("/api/introspect/workers/w1")
        assert resp.status_code == HTTPStatus.NO_CONTENT
        manager.delete_worker.assert_awaited_once_with("w1", reason="admin_kill")

    def test_worker_not_found(self, redis, manager):
        redis.hgetall = AsyncMock(return_value={})
        app = _make_app(redis=redis, worker_manager=manager)
        with TestClient(app) as c:
            resp = c.delete("/api/introspect/workers/nonexistent")
        assert resp.status_code == HTTPStatus.NOT_FOUND
