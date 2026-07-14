import json
from pathlib import Path

import httpx
from live_harness import (
    CleanupError,
    OwnershipManifest,
    cleanup_guard,
    resolve_repo_root,
    run_non_llm_qa,
)
import pipeline_helpers
from pipeline_helpers import build_github_cleanup_script
import pytest


def test_repo_root_is_derived_from_harness_location(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    harness = root / "tests" / "live" / "live_harness.py"
    harness.parent.mkdir(parents=True)
    (root / "pyproject.toml").touch()
    harness.touch()

    monkeypatch.delenv("ORCHESTRATOR_ROOT", raising=False)

    assert resolve_repo_root(harness) == root.resolve()


def test_repo_root_override_must_identify_repository(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCHESTRATOR_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="pyproject.toml"):
        resolve_repo_root(Path(__file__))


def test_manifest_cleanup_is_fail_closed_and_verifies_residue():
    manifest = OwnershipManifest(run_id="run-1")
    manifest.own("github_repository", "org/repo")
    calls = []

    def delete(resource):
        calls.append(("delete", resource.identifier))

    def exists(resource):
        calls.append(("exists", resource.identifier))
        return True

    with pytest.raises(CleanupError, match="still exists"):
        manifest.teardown(delete=delete, exists=exists)

    assert calls == [("delete", "org/repo"), ("exists", "org/repo")]


def test_manifest_reports_delete_failure_without_skipping_verification():
    manifest = OwnershipManifest(run_id="run-1")
    manifest.own("project", "project-1")
    verified = []

    def delete(resource):
        raise RuntimeError("delete failed")

    def exists(resource):
        verified.append(resource.identifier)
        return False

    with pytest.raises(CleanupError, match="delete failed"):
        manifest.teardown(delete=delete, exists=exists)

    assert verified == ["project-1"]


def test_github_cleanup_script_is_valid_python():
    script = build_github_cleanup_script("owned-repository")

    compile(script, "<github-cleanup>", "exec")
    assert "project-factory-organization/owned-repository" in script


@pytest.mark.asyncio
async def test_cleanup_guard_runs_when_qa_fails_before_fixture_yield():
    cleaned = []

    async def cleanup():
        cleaned.append(True)

    with pytest.raises(RuntimeError, match="QA failed"):
        async with cleanup_guard(cleanup):
            raise RuntimeError("QA failed")

    assert cleaned == [True]


@pytest.mark.asyncio
async def test_cleanup_guard_preserves_run_and_cleanup_failures():
    async def cleanup():
        raise CleanupError("residue")

    with pytest.raises(BaseExceptionGroup) as caught:
        async with cleanup_guard(cleanup):
            raise RuntimeError("QA failed")

    assert [str(error) for error in caught.value.exceptions] == ["QA failed", "residue"]


@pytest.mark.asyncio
async def test_partial_project_creation_writes_manifest_and_cleans_up(monkeypatch, tmp_path):
    cleanup_contexts = []

    async def cleanup(api, api_no_auth, ctx):
        cleanup_contexts.append(ctx)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/":
            return httpx.Response(201, json={"id": "project"})
        return httpx.Response(500, text="repository unavailable")

    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setattr(pipeline_helpers, "cleanup_all", cleanup)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(AssertionError, match="Create repository failed"):
            await pipeline_helpers.create_noop_project(api)

    assert len(cleanup_contexts) == 1
    manifest = cleanup_contexts[0]["manifest"]
    assert [(resource.kind, resource.identifier) for resource in manifest.resources] == [
        ("project", cleanup_contexts[0]["project_id"])
    ]
    written = json.loads((tmp_path / ".live-manifests" / f"{manifest.run_id}.json").read_text())
    assert written["resources"] == [{"identifier": manifest.run_id, "kind": "project"}]


@pytest.mark.asyncio
async def test_qa_gate_requires_separate_passed_terminal_run():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        result = await run_non_llm_qa(api, "http://deployed", timeout=1, poll_interval=0)

    assert result["status"] == "completed"
    assert result["qa_outcome"] == "passed"


@pytest.mark.asyncio
async def test_qa_gate_rejects_non_passing_terminal_outcome():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
        with pytest.raises(AssertionError, match="status=failed outcome=failed"):
            await run_non_llm_qa(api, "http://deployed", timeout=0.001, poll_interval=0)
