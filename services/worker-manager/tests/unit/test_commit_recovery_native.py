"""Actual manager route and native Git; only transport is mapped to local bare Git."""

import base64
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from threading import Thread
from unittest.mock import AsyncMock

import httpx
import pytest
from test_commit_recovery import git, owned_checkout as checkout_fixture

from shared.contracts.dto.commit_publication import PublicationFailure
from shared.workspace_preservation import has_preserved_work
from src.routers import commit_recovery

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess

owned_checkout = checkout_fixture


@pytest.fixture
def local_transport(owned_checkout, tmp_path, monkeypatch):
    req, _, _ = owned_checkout
    remote = tmp_path / "transport.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    native = subprocess.run
    calls = []

    def transport(argv, **kwargs):
        # The publisher/inspection remain real. Replace only the owned origin at
        # the native transport boundary, never import source configuration.
        if "ls-remote" in argv or "push" in argv:
            calls.append((argv, kwargs))
            argv = [str(remote) if arg == "origin" else arg for arg in argv]
            kwargs = {**kwargs, "env": {**kwargs["env"], "GIT_ALLOW_PROTOCOL": "file"}}
        return native(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", transport)
    return req, remote, calls


@pytest.mark.parametrize("malicious", ["proxy_askpass", "include_helper_hook"])
async def test_manager_never_executes_worker_context(local_transport, malicious, monkeypatch):
    req, remote, calls = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    marker = work.parent / "manager-secret-stolen"
    evil = work / "evil.sh"
    evil.write_text(f"#!/bin/sh\nenv > '{marker}'\nexit 1\n")
    evil.chmod(0o700)
    if malicious == "proxy_askpass":
        git(work, "config", "http.proxy", "http://x@127.0.0.1:1")
        git(work, "config", "core.askPass", str(evil))
    else:
        include = work / "evil.config"
        include.write_text(f"[core]\n hooksPath = {work / 'evil-hooks'}\n")
        git(work, "config", "include.path", str(include))
        hooks = work / "evil-hooks"
        hooks.mkdir()
        (hooks / "pre-push").symlink_to(evil)
        git(work, "config", "remote.origin.url", f"ext::{evil}")
        git(work, "config", "credential.helper", f"!{evil}")
    # Ambient platform and Git environment are also outside the clean context.
    monkeypatch.setenv("PLATFORM_SENTINEL_SECRET", "manager-only-secret")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(work / "evil.config"))
    before = (work / ".git/config").read_bytes()
    for _ in range(2):
        receipt = await commit_recovery.publish_preserved_commit(
            "eng-owned", req, "fixture-internal"
        )
        assert receipt.published, receipt
    sha = req.app.state.fixture_claim["commit_sha"]
    assert git(remote, "rev-parse", "refs/heads/story/story-owned") == sha
    assert not marker.exists()
    assert (work / ".git/config").read_bytes() == before
    assert len({c[1]["env"]["GIT_CONFIG_VALUE_1"] for c in calls}) == 2
    for argv, kwargs in calls:
        assert "manager-only-secret" not in str(kwargs)
        assert "synthetic-fresh" not in str(argv)
        assert kwargs["cwd"] != work
        assert "PLATFORM_SENTINEL_SECRET" not in kwargs["env"]
        assert not Path(kwargs["cwd"]).exists()  # manager snapshot cleaned


@pytest.mark.parametrize(
    "layout",
    [
        "workspace_symlink",
        "object_symlink",
        "alternates",
        "missing",
        "changed_head",
        "replace_refs",
        "linked_git",
        "wrong_branch",
        "grafts",
        "promisor",
        "corrupt",
        "oversized",
        "fifo",
    ],
)
async def test_manager_refuses_unsafe_source_before_mint(local_transport, layout):
    req, _, calls = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    if layout == "workspace_symlink":
        moved = work.with_name("outside")
        work.rename(moved)
        work.symlink_to(moved, target_is_directory=True)
    elif layout == "object_symlink":
        object_file = next((work / ".git/objects").glob("??/*"))
        moved = work.parent / "foreign-object"
        object_file.rename(moved)
        object_file.symlink_to(moved)
    elif layout == "alternates":
        (work / ".git/objects/info/alternates").write_text("/untrusted/objects\n")
    elif layout == "missing":
        sha = req.app.state.fixture_claim["commit_sha"]
        (work / f".git/objects/{sha[:2]}/{sha[2:]}").unlink()
    elif layout == "changed_head":
        (work / "app.txt").write_text("newer")
        git(work, "commit", "-am", "newer")
    elif layout == "replace_refs":
        (work / ".git/refs/replace").mkdir()
    elif layout == "wrong_branch":
        git(work, "checkout", "-b", "foreign/branch")
    elif layout == "grafts":
        (work / ".git/info/grafts").write_text(req.app.state.fixture_claim["commit_sha"] + "\n")
    elif layout == "promisor":
        (work / (".git/objects/pack/pack-" + "a" * 40 + ".promisor")).touch()
    elif layout in {"corrupt", "oversized", "fifo"}:
        sha = req.app.state.fixture_claim["commit_sha"]
        obj = work / f".git/objects/{sha[:2]}/{sha[2:]}"
        obj.unlink()  # Native loose objects are read-only; replace the fixture inode.
        if layout == "corrupt":
            obj.write_bytes(b"corrupt sentinel object")
        elif layout == "oversized":
            with obj.open("wb") as handle:
                handle.truncate(256 * 1024 * 1024 + 1)
        else:
            os.mkfifo(obj)
    else:
        gitdir = work / ".git"
        gitdir.rename(work.parent / "foreign-git")
        gitdir.write_text(f"gitdir: {work.parent / 'foreign-git'}\n")
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert not receipt.published
    assert receipt.failure in {
        PublicationFailure.INSPECTION_FAILED,
        PublicationFailure.OBJECT_MISSING,
        PublicationFailure.HEAD_CHANGED,
        PublicationFailure.WRONG_BRANCH,
    }
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()
    assert calls == [] and work.exists()


def test_preservation_inspection_never_invokes_source_git(owned_checkout, monkeypatch):
    req, _, _ = owned_checkout
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    native = subprocess.run

    def assert_clean(argv, **kwargs):
        assert Path(kwargs["cwd"]) != work
        assert kwargs["env"].get("HOME") != os.environ["HOME"]
        return native(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", assert_clean)
    assert has_preserved_work(work)


async def test_proxy_askpass_and_include_cannot_receive_fresh_http_credentials(
    owned_checkout, tmp_path, monkeypatch
):
    req, _, _ = owned_checkout
    from shared.clients.github import GitHubAppClient

    github = GitHubAppClient()
    github._installation_cache[("fixture", "owned")] = 123
    github._token_cache[(123, "owned")] = (
        "expired-sentinel",
        datetime.now(UTC) - timedelta(seconds=1),
    )
    monkeypatch.setattr(github, "_generate_jwt", lambda: "synthetic-app-jwt")
    mint = AsyncMock(
        side_effect=[
            httpx.Response(
                201,
                json={
                    "token": token,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            )
            for token in ("synthetic-fresh-one", "synthetic-fresh-two")
        ]
    )
    monkeypatch.setattr(github, "_make_request", mint)
    req.app.state.github = github
    work = tmp_path / "repo-owned"
    remote = tmp_path / "owned.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(remote, "config", "http.receivepack", "true")
    marker = tmp_path / "stolen"
    evil = work / "evil.sh"
    evil.write_text(f"#!/bin/sh\nenv > '{marker}'\nexit 1\n")
    evil.chmod(0o700)
    include = work / "evil.config"
    include.write_text(f"[core]\naskPass = {evil}\n[http]\nproxy = http://x@127.0.0.1:1\n")
    git(work, "config", "include.path", str(include))
    native = subprocess.run
    credentials = []

    class GitHTTP(BaseHTTPRequestHandler):
        def serve(self):
            credentials.append(self.headers.get("Authorization"))
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response = native(
                ["/usr/bin/git", "http-backend"],
                input=body,
                capture_output=True,
                check=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "GIT_PROJECT_ROOT": str(tmp_path),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "REQUEST_METHOD": self.command,
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "REMOTE_USER": "fixture",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                },
            ).stdout
            headers, _, data = response.partition(b"\r\n\r\n")
            self.send_response(200)
            for line in headers.splitlines():
                key, _, value = line.decode().partition(": ")
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = serve
        do_POST = serve

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), GitHTTP)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    local = f"http://127.0.0.1:{server.server_port}/owned.git"

    def transport(argv, **kwargs):
        if "ls-remote" in argv or "push" in argv:
            # Test-owned transport mapping only. Production always uses its
            # exact server-owned HTTPS URL; native config/objects stay clean.
            assert kwargs["cwd"] != work
            argv = [
                argv[0],
                "-c",
                f"url.{local}.insteadOf=https://github.com/fixture/owned.git",
                *argv[1:],
            ]
            env = {
                **kwargs["env"],
                "GIT_ALLOW_PROTOCOL": "http",
                "GIT_CONFIG_KEY_1": f"http.{local}.extraheader",
            }
            kwargs = {**kwargs, "env": env}
        return native(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", transport)
    try:
        for _ in range(2):
            receipt = await commit_recovery.publish_preserved_commit(
                "eng-owned", req, "fixture-internal"
            )
            assert receipt.published, receipt
            github._token_cache[(123, "owned")] = (
                "expired-sentinel",
                datetime.now(UTC) - timedelta(seconds=1),
            )
        assert set(credentials) == {
            "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
            for token in ("synthetic-fresh-one", "synthetic-fresh-two")
        }
        assert not marker.exists()
        assert mint.await_count == 2
        assert all(
            call.kwargs["json"] == {"repositories": ["owned"]} for call in mint.await_args_list
        )
        assert (
            git(remote, "rev-parse", "refs/heads/story/story-owned")
            == req.app.state.fixture_claim["commit_sha"]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_changed_source_during_snapshot_refuses_before_token(local_transport, monkeypatch):
    from shared.git_snapshot import _Source

    req, _, calls = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    capture = _Source.capture

    def mutate(self, destination=None):
        result = capture(self, destination)
        if destination is not None:
            ref = work / ".git/refs/heads/story/story-owned"
            ref.write_text("c" * 40 + "\n")
        return result

    monkeypatch.setattr(_Source, "capture", mutate)
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is PublicationFailure.HEAD_CHANGED
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()
    assert not calls and work.exists()


async def test_wrong_server_repository_refuses_before_token(local_transport):
    req, _, calls = local_transport
    req.app.state.fixture_claim["identity"]["repository_url"] = (
        "https://github.com/foreign/repo.git"
    )
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is PublicationFailure.OWNERSHIP_MISSING
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()
    assert not calls


async def test_packed_source_and_cloned_remote_head_publish_exact_sha(local_transport):
    req, remote, _ = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    sha = req.app.state.fixture_claim["commit_sha"]
    git(
        work,
        "update-ref",
        "refs/remotes/origin/main",
        req.app.state.fixture_claim["identity"]["baseline"],
    )
    git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(work, "gc")
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.published, receipt
    assert git(remote, "rev-parse", "refs/heads/story/story-owned") == sha


async def test_manager_readback_mismatch_and_lost_push_response(local_transport, monkeypatch):
    req, remote, _ = local_transport
    transport = subprocess.run
    baseline = req.app.state.fixture_claim["identity"]["baseline"]

    def stale(argv, **kwargs):
        if "ls-remote" in argv:
            return subprocess.CompletedProcess(
                argv, 0, f"{baseline}\trefs/heads/story/story-owned\n", ""
            )
        return transport(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", stale)
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is PublicationFailure.READBACK_MISMATCH
    sha = req.app.state.fixture_claim["commit_sha"]
    assert git(remote, "rev-parse", "refs/heads/story/story-owned") == sha
    monkeypatch.setattr(subprocess, "run", transport)
    replay = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert replay.published and replay.remote_sha == sha


async def test_manager_lost_push_reply_is_proved_by_native_readback(local_transport, monkeypatch):
    req, remote, _ = local_transport
    transport = subprocess.run

    def lost(argv, **kwargs):
        result = transport(argv, **kwargs)
        if "push" in argv:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return result

    monkeypatch.setattr(subprocess, "run", lost)
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    sha = req.app.state.fixture_claim["commit_sha"]
    assert receipt.published and receipt.remote_sha == sha
    assert git(remote, "rev-parse", "refs/heads/story/story-owned") == sha


@pytest.mark.parametrize("change", ["source", "source_layout", "lease"])
async def test_changed_checkout_during_mint_never_pushes(local_transport, change):
    req, _, calls = local_transport

    async def mint(*args):
        if change in {"source", "source_layout"}:
            work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
            if change == "source":
                (work / ".git/refs/heads/story/story-owned").write_text("c" * 40 + "\n")
            else:
                (work / ".git").rename(work / "moved-git")
        else:
            await req.app.state.redis.set(
                "workspace:lock:00000000-0000-0000-0000-000000000001", "late-worker"
            )
        return "synthetic-fresh-one"

    req.app.state.github.get_repo_scoped_token.side_effect = mint
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure in {PublicationFailure.HEAD_CHANGED, PublicationFailure.STALE_ATTEMPT}
    assert not calls


@pytest.mark.parametrize("content", ["injected", "no_new", "invalid_baseline"])
async def test_manager_validates_range_before_obtaining_credentials(local_transport, content):
    req, _, calls = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    if content == "injected":
        (work / "TASK.md").write_text("Injected agent instructions")
        git(work, "add", "TASK.md")
        git(work, "commit", "-m", "injected")
        req.app.state.fixture_claim["commit_sha"] = git(work, "rev-parse", "HEAD")
        expected = PublicationFailure.INJECTED_PATHS
    elif content == "no_new":
        req.app.state.fixture_claim["identity"]["baseline"] = req.app.state.fixture_claim[
            "commit_sha"
        ]
        expected = PublicationFailure.NO_NEW_COMMIT
    else:
        req.app.state.fixture_claim["identity"]["baseline"] = "f" * 40
        expected = PublicationFailure.INSPECTION_FAILED
    receipt = await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    assert receipt.failure is expected
    req.app.state.github.get_repo_scoped_token.assert_not_awaited()
    assert not calls


async def test_manager_copies_objects_without_linking_source(local_transport, monkeypatch):
    req, _, _ = local_transport
    work = Path(req.app.state.scaffolded_workspace_path) / "repo-owned"
    publish = commit_recovery.publish_commit

    def inspect_copy(clean, *args, **kwargs):
        for original in (work / ".git/objects").glob("??/*"):
            copied = clean / original.relative_to(work)
            assert copied.read_bytes() == original.read_bytes()
            assert (copied.stat().st_dev, copied.stat().st_ino) != (
                original.stat().st_dev,
                original.stat().st_ino,
            )
        assert not (clean / "app.txt").exists()
        assert not (clean / ".git/hooks").exists()
        return publish(clean, *args, **kwargs)

    monkeypatch.setattr(commit_recovery, "publish_commit", inspect_copy)
    assert (
        await commit_recovery.publish_preserved_commit("eng-owned", req, "fixture-internal")
    ).published
