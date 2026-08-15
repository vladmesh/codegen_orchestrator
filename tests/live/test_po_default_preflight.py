"""Offline contract for the matrix PO-default evidence preflight."""

from __future__ import annotations

import json
from pathlib import Path

import po_default_preflight
import pytest

pytestmark = pytest.mark.needs_no_api_credential


class FakeRuntime:
    def __init__(
        self, *, notification_after: dict | None = None, fail_explicit: bool = False
    ) -> None:
        self.runtime_default = "codex"
        self.notification_after = notification_after or {"proactive_matching_entries": 0}
        self.fail_explicit = fail_explicit
        self.tool_calls: list[tuple[dict, dict]] = []
        self.projects: dict[str, dict] = {}
        self.repositories: dict[str, dict] = {}
        self.manifests: list[dict] = []
        self.cleanup_calls: list[dict] = []
        self._snapshot_count = 0

    async def require_test_user(self, telegram_id: str) -> None:
        assert telegram_id == "999000001"

    def read_runtime_default(self) -> str:
        return self.runtime_default

    def write_manifest(self, manifest) -> None:
        self.manifests.append(
            {
                "run_id": manifest.run_id,
                "resources": [
                    (resource.kind, resource.identifier) for resource in manifest.resources
                ],
            }
        )

    async def invoke_create_project(self, arguments: dict, config: dict) -> str:
        self.tool_calls.append((arguments, config))
        identity = config["configurable"]["project_creation_identity"]
        project_id = identity["project_id"]
        if arguments.get("agent_type") and self.fail_explicit:
            raise RuntimeError("explicit invocation failed")
        agent = arguments.get("agent_type", self.runtime_default)
        self.projects[project_id] = {
            "id": project_id,
            "initiating_run_id": identity["initiating_run_id"],
            "config": {"agent_type": agent},
        }
        repo_id = f"repo-{project_id[-4:]}"
        self.repositories[project_id] = {"id": repo_id, "project_id": project_id}
        return f"Project created. ID: {project_id}, Title: Matrix, Slug: matrix"

    async def read_project(self, project_id: str, config: dict) -> dict:
        return self.projects[project_id]

    async def read_repositories(self, project_id: str) -> list[dict]:
        return [self.repositories[project_id]]

    async def notification_snapshot(self, *, request_ids: list[str], telegram_id: str) -> dict:
        assert all(request_id.startswith("matrix-po-default-") for request_id in request_ids)
        assert telegram_id == "999000001"
        self._snapshot_count += 1
        streams = dict.fromkeys(request_ids, False)
        if self._snapshot_count == 1:
            return {"po_response_streams": streams, "proactive_matching_entries": 0}
        return {"po_response_streams": streams, **self.notification_after}

    async def notification_outbox(self, project_ids: list[str]) -> dict:
        return {
            project_id: {"run_count": 0, "owner_notification_count": 0}
            for project_id in project_ids
        }

    async def cleanup(self, ctx: dict) -> dict:
        resources = {(item.kind, item.identifier) for item in ctx["manifest"].resources}
        assert ("project", ctx["project_id"]) in resources
        self.cleanup_calls.append(ctx)
        self.projects.pop(ctx["project_id"], None)
        self.repositories.pop(ctx["project_id"], None)
        return {
            "status": "clean",
            "run_cleanup": {"remaining_containers": [], "remaining_networks": []},
        }

    async def close(self) -> None:
        return None


def _config(tmp_path: Path) -> po_default_preflight.PreflightConfig:
    return po_default_preflight.PreflightConfig(
        preflight_id="matrix-po-default-42-1",
        checkout_sha="a" * 40,
        test_telegram_id="999000001",
        output_directory=tmp_path,
    )


@pytest.mark.asyncio
async def test_preflight_proves_omission_and_explicit_preservation_with_bound_artifact(tmp_path):
    runtime = FakeRuntime()

    artifact = await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    assert [arguments for arguments, _ in runtime.tool_calls] == [
        {"title": "matrix-po-default", "modules": "backend", "description": "matrix-po-default"},
        {
            "title": "matrix-po-explicit",
            "modules": "backend",
            "description": "matrix-po-default",
            "agent_type": "claude",
        },
    ]
    assert all("agent_type" not in runtime.tool_calls[0][0] for _ in [None])
    assert runtime.manifests[0]["resources"] == [("project", artifact["projects"][0]["id"])]
    assert artifact["checkout_sha"] == "a" * 40
    assert artifact["omitted_argument_assertion"] == {
        "agent_type_present": False,
        "argument_keys": ["description", "modules", "title"],
    }
    assert artifact["projects"][0]["persisted_agent_type"] == "codex"
    assert artifact["projects"][1]["persisted_agent_type"] == "claude"
    assert artifact["projects"][0]["initiating_run_id"].endswith("-omitted")
    assert artifact["projects"][1]["initiating_run_id"].endswith("-explicit")
    assert artifact["notification_probes"]["after"]["proactive_matching_entries"] == 0
    assert all(
        exists is False
        for exists in artifact["notification_probes"]["after"]["po_response_streams"].values()
    )
    assert len(runtime.cleanup_calls) == 2

    evidence_path = tmp_path / "run-evidence-po-default-matrix-po-default-42-1.json"
    assert json.loads(evidence_path.read_text()) == artifact
    assert "INTERNAL_API_KEY" not in evidence_path.read_text()


@pytest.mark.asyncio
async def test_notification_ambiguity_fails_closed_and_cleans_pre_registered_projects(tmp_path):
    runtime = FakeRuntime(notification_after={"proactive_matching_entries": 1})

    with pytest.raises(po_default_preflight.PreflightError, match="notification"):
        await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    assert len(runtime.cleanup_calls) == 2
    assert all(call["project_id"] not in runtime.projects for call in runtime.cleanup_calls)
    artifact = json.loads(
        (tmp_path / "run-evidence-po-default-matrix-po-default-42-1.json").read_text()
    )
    assert artifact["status"] == "failed"
    assert artifact["failure"]["kind"] == "PreflightError"
    assert artifact["cleanup"]["omitted"]["status"] == "clean"
    assert artifact["cleanup"]["explicit"]["status"] == "clean"


@pytest.mark.asyncio
async def test_explicit_tool_error_retains_the_first_owned_project_and_cleans_both_paths(tmp_path):
    runtime = FakeRuntime(fail_explicit=True)

    with pytest.raises(po_default_preflight.PreflightError, match="RuntimeError"):
        await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    artifact = json.loads(
        (tmp_path / "run-evidence-po-default-matrix-po-default-42-1.json").read_text()
    )
    assert [project["persisted_agent_type"] for project in artifact["projects"]] == ["codex"]
    assert len(runtime.cleanup_calls) == 2
    assert artifact["cleanup"]["omitted"]["status"] == "clean"
    assert artifact["cleanup"]["explicit"]["status"] == "clean"


@pytest.mark.asyncio
async def test_artifact_identity_is_exclusive_so_a_later_matrix_step_cannot_overwrite_it(tmp_path):
    config = _config(tmp_path)
    await po_default_preflight.run_preflight(FakeRuntime(), config)
    later_step = FakeRuntime()

    with pytest.raises(FileExistsError):
        await po_default_preflight.run_preflight(later_step, config)

    assert later_step.tool_calls == []


def test_workflow_runs_the_preflight_once_before_the_worker_qa_combinations():
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "agent-matrix.yml"
    source = workflow.read_text(encoding="utf-8")

    command = "uv run python tests/live/po_default_preflight.py"
    assert source.count(command) == 1
    assert source.index(command) < source.index("for qa_agent in claude codex; do")
    assert 'PO_DEFAULT_MATRIX_API_CONTAINER="$matrix_api_container"' in source
    assert 'PO_DEFAULT_MATRIX_CHECKOUT_SHA="${{ github.sha }}"' in source
    assert "run-evidence-po-default-" in source


def test_entry_point_uses_the_po_tool_and_existing_manifest_cleanup_boundary():
    source = (Path(__file__).resolve().parent / "po_default_preflight.py").read_text(
        encoding="utf-8"
    )

    assert 'manifest.own("project", project_id' in source
    assert source.index('manifest.own("project", project_id') < source.index(
        "await runtime.invoke_create_project"
    )
    assert "await self._create_project.ainvoke(arguments, config=config)" in source
    assert 'post_raw("projects/' not in source
    assert "await pipeline_helpers.cleanup_all" in source
