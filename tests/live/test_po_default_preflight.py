"""Offline contract for the matrix PO-default evidence preflight."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock

import po_default_preflight
import pytest

pytestmark = pytest.mark.needs_no_api_credential


class FakeRuntime:
    def __init__(
        self,
        *,
        proactive_delta_kind: str | None = None,
        historical_proactive_entries: int = 0,
        fail_explicit: bool = False,
    ) -> None:
        self.runtime_default = "codex"
        self.proactive_delta_kind = proactive_delta_kind
        self.historical_proactive_entries = historical_proactive_entries
        self.fail_explicit = fail_explicit
        self.tool_calls: list[tuple[dict, dict]] = []
        self.projects: dict[str, dict] = {}
        self.repositories: dict[str, dict] = {}
        self.manifests: list[dict] = []
        self.cleanup_calls: list[dict] = []
        self.events: list[str] = []
        self._snapshot_count = 0

    async def ensure_test_user(self, telegram_id: str) -> None:
        assert telegram_id == "999000001"

    def read_runtime_default(self) -> str:
        return self.runtime_default

    def write_manifest(self, manifest) -> None:
        self.events.append("write_manifest")
        self.manifests.append(
            {
                "run_id": manifest.run_id,
                "resources": [
                    (resource.kind, resource.identifier) for resource in manifest.resources
                ],
            }
        )

    async def invoke_create_project(self, arguments: dict, config: dict) -> str:
        self.events.append("invoke_create_project")
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

    async def notification_snapshot(
        self,
        *,
        request_ids: list[str],
        project_ids: list[str],
        telegram_id: str,
        phase: str,
        proactive_after_id: str | None,
    ) -> dict:
        assert all(request_id.startswith("matrix-po-default-") for request_id in request_ids)
        assert len(project_ids) == 2
        assert telegram_id == "999000001"
        self._snapshot_count += 1
        streams = dict.fromkeys(request_ids, False)
        if phase == "before":
            assert proactive_after_id is None
            return {
                "po_response_streams": streams,
                "proactive_boundary": {
                    "stream_id": "100-0" if self.historical_proactive_entries else None,
                },
            }

        assert phase == "after"
        assert proactive_after_id == ("100-0" if self.historical_proactive_entries else None)
        delta: list[dict[str, str | None]] = []
        if self.proactive_delta_kind == "owned":
            delta = [
                {
                    "stream_id": "101-0",
                    "project_id": project_ids[0],
                    "project_identity": "valid",
                }
            ]
        elif self.proactive_delta_kind == "ambiguous":
            delta = [
                {
                    "stream_id": "101-0",
                    "project_id": None,
                    "project_identity": "missing",
                }
            ]
        elif self.proactive_delta_kind == "unrelated":
            delta = [
                {
                    "stream_id": "101-0",
                    "project_id": "22222222-2222-2222-2222-222222222222",
                    "project_identity": "valid",
                }
            ]
        return {"po_response_streams": streams, "proactive_delta": delta}

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
    runtime = FakeRuntime(historical_proactive_entries=3)

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
    assert runtime.manifests[0]["resources"] == [("project", artifact["projects"][0]["id"])]
    assert runtime.events[:3] == ["write_manifest", "write_manifest", "invoke_create_project"]
    assert artifact["checkout_sha"] == "a" * 40
    assert artifact["omitted_argument_assertion"] == {
        "agent_type_present": False,
        "argument_keys": ["description", "modules", "title"],
    }
    assert artifact["projects"][0]["persisted_agent_type"] == "codex"
    assert artifact["projects"][1]["persisted_agent_type"] == "claude"
    assert artifact["projects"][0]["initiating_run_id"].endswith("-omitted")
    assert artifact["projects"][1]["initiating_run_id"].endswith("-explicit")
    assert artifact["notification_probes"]["before"]["proactive_boundary"] == {
        "stream_id": "100-0",
    }
    assert artifact["notification_probes"]["after"]["proactive_delta"] == []
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
    runtime = FakeRuntime(proactive_delta_kind="ambiguous")

    with pytest.raises(po_default_preflight.PreflightError, match="notification"):
        await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    assert len(runtime.cleanup_calls) == 2
    assert all(call["project_id"] not in runtime.projects for call in runtime.cleanup_calls)
    artifact = json.loads(
        (tmp_path / "run-evidence-po-default-matrix-po-default-42-1.json").read_text()
    )
    assert artifact["status"] == "failed"
    assert artifact["failure"]["kind"] == "PreflightError"
    assert "ambiguous" in artifact["failure"]["message"]
    assert artifact["notification_probes"]["after"]["proactive_delta"] == [
        {"stream_id": "101-0", "project_id": None, "project_identity": "missing"}
    ]
    assert artifact["cleanup"]["omitted"]["status"] == "clean"
    assert artifact["cleanup"]["explicit"]["status"] == "clean"


@pytest.mark.asyncio
async def test_historical_proactive_entries_do_not_fail_the_preflight(tmp_path):
    runtime = FakeRuntime(historical_proactive_entries=8)

    artifact = await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    assert artifact["status"] == "passed"
    assert artifact["notification_probes"]["before"]["proactive_boundary"] == {"stream_id": "100-0"}


@pytest.mark.asyncio
async def test_unrelated_post_boundary_proactive_entry_does_not_fail_the_preflight(tmp_path):
    runtime = FakeRuntime(proactive_delta_kind="unrelated")

    artifact = await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    assert artifact["status"] == "passed"
    assert artifact["notification_probes"]["after"]["proactive_delta"] == [
        {
            "stream_id": "101-0",
            "project_id": "22222222-2222-2222-2222-222222222222",
            "project_identity": "valid",
        }
    ]


@pytest.mark.asyncio
async def test_owned_post_boundary_proactive_entry_fails_and_is_owned_for_cleanup(tmp_path):
    runtime = FakeRuntime(proactive_delta_kind="owned")

    with pytest.raises(po_default_preflight.PreflightError, match="owned"):
        await po_default_preflight.run_preflight(runtime, _config(tmp_path))

    artifact = json.loads(
        (tmp_path / "run-evidence-po-default-matrix-po-default-42-1.json").read_text()
    )
    owned_entry = artifact["notification_probes"]["after"]["proactive_delta"][0]
    assert owned_entry["project_id"] == artifact["projects"][0]["id"]
    assert ("redis_entry", "101-0") in {
        resource
        for call in runtime.cleanup_calls
        for resource in {(item.kind, item.identifier) for item in call["manifest"].resources}
    }
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
    assert artifact["failure"] == {
        "kind": "RuntimeError",
        "message": "explicit invocation failed",
    }
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


def test_preflight_identity_reserves_space_for_the_longest_variant(tmp_path):
    prefix = "a" * (64 - len("-explicit"))

    config = po_default_preflight.PreflightConfig(
        preflight_id=prefix,
        checkout_sha="a" * 40,
        test_telegram_id="999000001",
        output_directory=tmp_path,
    )

    assert config.preflight_id == prefix
    with pytest.raises(po_default_preflight.PreflightError, match="manifest run suffix"):
        po_default_preflight.PreflightConfig(
            preflight_id=prefix + "a",
            checkout_sha="a" * 40,
            test_telegram_id="999000001",
            output_directory=tmp_path,
        )


@pytest.mark.parametrize(
    ("message", "redacted"),
    [
        ("token=super-secret-value", "token=<redacted>"),
        ("Authorization: Bearer super-secret-value", "Authorization=<redacted>"),
    ],
)
def test_failure_receipt_redacts_sensitive_values(message, redacted):
    assert po_default_preflight._failure_receipt(RuntimeError(message)) == {
        "kind": "RuntimeError",
        "message": redacted,
    }


def test_workflow_runs_the_preflight_once_before_the_worker_qa_combinations():
    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "agent-matrix.yml"
    source = workflow.read_text(encoding="utf-8")

    command = "uv run python tests/live/po_default_preflight.py"
    assert source.count(command) == 1
    assert source.index(command) < source.index("for qa_agent in claude codex; do")
    assert 'PO_DEFAULT_MATRIX_API_CONTAINER="$matrix_api_container"' in source
    assert 'PO_DEFAULT_MATRIX_CHECKOUT_SHA="${{ github.sha }}"' in source
    assert "run-evidence-po-default-" in source


def test_po_tool_boundary_binds_to_langgraph_in_a_workspace_interpreter():
    """The import the workflow actually performs, in a process shaped like its own.

    Every other runtime test builds the object with `object.__new__`, so none of
    them executes this import — which is how a matrix run reached production and
    died on `No module named 'src.agents'`. A child interpreter is used because
    the pytest process has already resolved `src` for other modules, and a
    cached binding would hide exactly the failure under test.
    """
    root = Path(__file__).resolve().parents[2]
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(root / 'tests' / 'live')!r})\n"
        "from po_default_preflight import load_po_tool_boundary\n"
        f"load_po_tool_boundary(Path({str(root)!r}))\n"
        "from src.agents.po import tools_projects\n"
        "print(tools_projects.__file__)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (
        Path(result.stdout.strip())
        == root / "services" / "langgraph" / "src" / "agents" / "po" / "tools_projects.py"
    )


@pytest.mark.asyncio
async def test_matrix_runtime_invokes_the_released_po_tool_without_rewriting_arguments():
    runtime = object.__new__(po_default_preflight.MatrixRuntime)
    runtime._create_project = MagicMock()
    runtime._create_project.ainvoke = AsyncMock(
        return_value="Project created. ID: 1, Title: Matrix"
    )
    arguments = {"title": "matrix-po-default", "modules": "backend"}
    config = {"configurable": {"telegram_chat_id": "999000001"}}

    result = await runtime.invoke_create_project(arguments, config)

    assert result.startswith("Project created.")
    runtime._create_project.ainvoke.assert_awaited_once_with(arguments, config=config)
    assert "agent_type" not in arguments


@pytest.mark.asyncio
async def test_matrix_runtime_uses_the_harness_identity_setup(monkeypatch):
    import pipeline_helpers

    runtime = object.__new__(po_default_preflight.MatrixRuntime)
    runtime._api_url = "http://127.0.0.1:8000"
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=object())
    client.__aexit__ = AsyncMock(return_value=None)
    create_client = MagicMock(return_value=client)
    ensure = AsyncMock()
    monkeypatch.setattr(pipeline_helpers, "api_client_as_test_user", create_client)
    monkeypatch.setattr(pipeline_helpers, "ensure_test_user", ensure)

    await runtime.ensure_test_user(str(pipeline_helpers.TEST_TELEGRAM_ID))

    create_client.assert_called_once_with(base_url="http://127.0.0.1:8000")
    ensure.assert_awaited_once_with(client.__aenter__.return_value)
    with pytest.raises(po_default_preflight.PreflightError, match="live-harness test identity"):
        await runtime.ensure_test_user("1")


@pytest.mark.asyncio
async def test_matrix_runtime_scopes_the_proactive_probe_to_the_post_boundary_delta():
    runtime = object.__new__(po_default_preflight.MatrixRuntime)
    project_id = "11111111-1111-1111-1111-111111111111"
    calls: list[tuple[str, ...]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
            subprocess.CompletedProcess(
                [], 0, stdout='[["100-0",["telegram_chat_id","999000001"]]]', stderr=""
            ),
            subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    f'[["101-0",["telegram_chat_id","999000001","project_id","{project_id}"]]]'
                ),
                stderr="",
            ),
        ]
    )

    def compose_redis(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return next(responses)

    runtime._compose_redis = compose_redis
    before = await runtime.notification_snapshot(
        request_ids=["request-omitted", "request-explicit"],
        project_ids=[project_id],
        telegram_id="999000001",
        phase="before",
        proactive_after_id=None,
    )
    after = await runtime.notification_snapshot(
        request_ids=["request-omitted", "request-explicit"],
        project_ids=[project_id],
        telegram_id="999000001",
        phase="after",
        proactive_after_id=before["proactive_boundary"]["stream_id"],
    )

    assert before == {
        "po_response_streams": {"request-omitted": False, "request-explicit": False},
        "proactive_boundary": {"stream_id": "100-0"},
    }
    assert after["proactive_delta"] == [
        {"stream_id": "101-0", "project_id": project_id, "project_identity": "valid"}
    ]
    assert calls[-1] == ("--json", "XRANGE", "po:proactive", "(100-0", "+")
