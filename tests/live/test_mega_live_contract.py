"""Offline contracts for the level-1 lifecycle's one fork: who develops the product.

`mega-noop` and `mega-live` are one class, `TestFullPipeline`, and differ only in
the developer `pipeline_helpers.level1_developer_agent_type` resolves — which is
decided by the child environment `scripts/stand_run.py` gives each suite. These
tests drive that whole chain offline: the runner's own `main` builds the child
environment, and `create_level1_bot_project` creates the project under it.
"""

import json

import httpx
import pipeline_helpers
import pytest

from scripts import stand_run
from shared.contracts.acceptance import parse_health_only_criteria
from shared.stand_deadlines import (
    ENGINEERING_TIMEOUT,
    LIVE_QA_RUN_TIMEOUT,
    LLM_ENGINEERING_TIMEOUT,
    QA_RUN_TIMEOUT,
)

pytestmark = pytest.mark.needs_no_api_credential

LLM_ENV_NAMES = stand_run.LLM_ENV_NAMES


class _FinishedPytest:
    """A pytest child that exits green at once, so only its environment matters."""

    stdout = iter(())
    returncode = 0
    pid = 1

    def wait(self, *, timeout=None):
        return 0


def _suite_child_environment(tmp_path, monkeypatch, argv: list[str]) -> dict[str, str]:
    """The environment `stand_run.main` starts the suite's pytest with.

    The parent carries a developer and a QA executor both in its own process
    environment and in the deployed `.env`, which is exactly what a stand host
    that last ran a paid suite looks like.
    """
    override = tmp_path / "deployed-service-images.compose.yml"
    override.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv(stand_run.SERVICE_RELEASE_OVERRIDE_ENV, str(override))
    parent = {"LIVE_WORKER_AGENT_TYPE": "claude", "LIVE_LLM_QA": "1", "LIVE_QA_AGENT_TYPE": "codex"}
    for name, value in parent.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(stand_run, "RUN_ROOT", tmp_path / "runs")
    monkeypatch.setattr(stand_run, "read_env_file", lambda _path: dict(parent))
    monkeypatch.setattr(stand_run, "preflight", lambda _env, _log: True)
    monkeypatch.setattr(stand_run, "ensure_qa_executor", lambda _env, _qa, _log: True)
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured.update(command=command, env=kwargs["env"])
        return _FinishedPytest()

    monkeypatch.setattr(stand_run.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(stand_run.sys, "argv", ["stand_run.py", *argv, "--skip-sweep"])

    assert stand_run.main() == 0
    return captured


def _adopt(monkeypatch, child_env: dict[str, str]) -> None:
    """Make this process see the agent environment the suite's child was given."""
    for name in LLM_ENV_NAMES:
        if name in child_env:
            monkeypatch.setenv(name, child_env[name])
        else:
            monkeypatch.delenv(name, raising=False)


def _project_api(requests: list[tuple[str, str, dict]]) -> httpx.AsyncClient:
    """The API `create_level1_bot_project` talks to: project, repository, bot binding."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/projects/":
            return httpx.Response(201, json={"id": body["id"], "slug": "live-test-level1"})
        if request.url.path == "/api/repositories/":
            return httpx.Response(201, json={"id": "repo-1"})
        if request.url.path.endswith("/telegram/token"):
            return httpx.Response(
                200,
                json={"status": "ok", "bot_username": "mega_e2e_codegen_bot", "checks": []},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")


async def _create_level1_project(monkeypatch, tmp_path) -> tuple[dict, list]:
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    monkeypatch.setenv(pipeline_helpers.STAND_PRODUCT_BOT_TOKEN_ENV, "123:not-a-real-token")
    monkeypatch.delenv(pipeline_helpers.TEMPLATE_REPO_ENV, raising=False)
    monkeypatch.delenv(pipeline_helpers.TEMPLATE_REF_ENV, raising=False)
    requests: list[tuple[str, str, dict]] = []
    async with _project_api(requests) as api:
        ctx = await pipeline_helpers.create_level1_bot_project(api, api)
    return ctx, requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "developer", "qa_executor"),
    [
        (["--suite", "mega-noop"], "noop", None),
        (["--suite", "mega-live", "--worker", "claude", "--qa", "codex"], "claude", "codex"),
        (["--suite", "mega-live", "--worker", "codex", "--qa", "claude"], "codex", "claude"),
    ],
)
async def test_each_suite_creates_the_level1_project_with_its_own_developer(
    tmp_path, monkeypatch, argv, developer, qa_executor
):
    """The runner's child environment is what decides the developer, per suite.

    `mega-noop` is started from a parent that carries `claude` in both its own
    environment and the deployed `.env`, and still creates a `noop` project:
    the runner gives a suite that spends no model no agent environment at all.
    """
    child = _suite_child_environment(tmp_path, monkeypatch, argv)

    assert child["command"][3] == "tests/live/test_full_pipeline.py::TestFullPipeline"
    _adopt(monkeypatch, child["env"])
    ctx, requests = await _create_level1_project(monkeypatch, tmp_path)

    project = next(body for method, path, body in requests if path == "/api/projects/")
    assert project["config"]["agent_type"] == developer
    assert ctx["agent_type"] == developer
    assert ctx["qa_requires_executor"] is (qa_executor is not None)
    assert child["env"].get("LIVE_QA_AGENT_TYPE") == qa_executor
    assert ctx["brief_variant"] == ("mega-noop" if developer == "noop" else "mega-live")


@pytest.mark.asyncio
async def test_a_developer_the_lifecycle_does_not_run_is_refused_before_anything_exists(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", "factory")

    with pytest.raises(pipeline_helpers.Level1RunRefused, match="LIVE_WORKER_AGENT_TYPE"):
        await _create_level1_project(monkeypatch, tmp_path)

    assert not (tmp_path / ".live-manifests").exists()


@pytest.mark.asyncio
async def test_a_model_developer_without_an_executor_qa_is_refused_before_anything_exists(
    tmp_path, monkeypatch
):
    """A model's code judged by `/health` alone would be a level-2 run proving level 1."""
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", "claude")
    monkeypatch.delenv("LIVE_LLM_QA", raising=False)
    monkeypatch.setenv("LIVE_QA_AGENT_TYPE", "codex")

    with pytest.raises(pipeline_helpers.Level1RunRefused, match="LIVE_LLM_QA"):
        await _create_level1_project(monkeypatch, tmp_path)

    assert not (tmp_path / ".live-manifests").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("developer", ["claude", "codex"])
async def test_a_live_run_s_descriptions_carry_the_contract_and_no_patch(
    tmp_path, monkeypatch, developer
):
    """What each of the three tasks tells a model, as the project is created with it."""
    monkeypatch.setenv("LIVE_WORKER_AGENT_TYPE", developer)
    monkeypatch.setenv("LIVE_LLM_QA", "1")
    monkeypatch.setenv("LIVE_QA_AGENT_TYPE", "claude")

    ctx, _ = await _create_level1_project(monkeypatch, tmp_path)

    descriptions = (
        ctx["task_description"],
        ctx["followup_task_description"],
        ctx["level1_extension_plan"]["task_description"],
    )
    for description in descriptions:
        assert "codegen-change-set" not in description
        assert ctx["level1_marker"] in description
    assert "level1_marker" in descriptions[0] and "/level1/marker" in descriptions[0]
    assert "/level1" in descriptions[1] and "setMyCommands" in descriptions[1]
    assert ctx["level1_extension_marker"] in descriptions[2]


def test_the_live_qa_criteria_need_an_executor_and_carry_this_run_s_markers(monkeypatch):
    """Neither story's checklist collapses into the health-only checks QA decides alone."""
    import level1_change_set

    first = level1_change_set.level1_qa_criteria("e2e-aaaaaaaaaaaa")
    second = level1_change_set.level1_extension_qa_criteria("e2e-aaaaaaaaaaaa", "e2e-bbbbbbbbbbbb")

    assert parse_health_only_criteria(first) is None
    assert parse_health_only_criteria(second) is None
    assert "e2e-aaaaaaaaaaaa" in first and "/level1/marker" in first
    assert "level1_marker" in first
    # The extension's checklist is the first story's, then its own.
    assert second.startswith(first)
    assert "e2e-bbbbbbbbbbbb" in second and "/level1/extension" in second
    assert "level1_extension_marker" in second


@pytest.mark.asyncio
async def test_a_live_story_writes_its_criteria_through_the_architect_s_repository_update():
    """`PATCH /api/repositories/{id}` — the update `update_acceptance_criteria` makes."""
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"id": "repo-1", **body})

    ctx = {"repo_id": "repo-1", "level1_qa_criteria": "- GET /level1/marker answers 200 prose"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as api:
        await pipeline_helpers._write_level1_qa_criteria(api, ctx)

    assert requests == [
        ("PATCH", "/api/repositories/repo-1", {"acceptance_criteria": ctx["level1_qa_criteria"]})
    ]
    assert ctx["level1_qa_criteria_written"] == ctx["level1_qa_criteria"]


@pytest.mark.asyncio
async def test_a_scripted_story_writes_no_criteria_and_a_dropped_write_names_admission():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"id": "repo-1", "acceptance_criteria": "- other"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as api:
        await pipeline_helpers._write_level1_qa_criteria(
            api, {"repo_id": "repo-1", "level1_qa_criteria": None}
        )
        assert requests == []
        with pytest.raises(pipeline_helpers.Level1PhaseFailed) as refused:
            await pipeline_helpers._write_level1_qa_criteria(
                api, {"repo_id": "repo-1", "level1_qa_criteria": "- mine"}
            )

    assert refused.value.phase == "admission"


def test_the_bounds_a_story_is_waited_on_follow_its_developer():
    scripted = {"agent_type": "noop", "qa_requires_executor": False}
    live = {"agent_type": "claude", "qa_requires_executor": True}

    assert pipeline_helpers.developer_engineering_timeout(scripted) == ENGINEERING_TIMEOUT
    assert pipeline_helpers.developer_engineering_timeout(live) == LLM_ENGINEERING_TIMEOUT
    assert pipeline_helpers.qa_run_timeout(scripted) == QA_RUN_TIMEOUT
    assert pipeline_helpers.qa_run_timeout(live) == LIVE_QA_RUN_TIMEOUT


def _branch(monkeypatch, *, ahead_by: int, diff: str) -> dict:
    """A story branch GitHub compares as `ahead_by` and whose own change is `diff`."""
    monkeypatch.setattr(
        pipeline_helpers,
        "probe_story_branch",
        lambda repo, branch: {"branch": branch, "status": "ahead", "ahead_by": ahead_by},
    )

    def record_diff(ctx):
        ctx["story_branch_diff_error"] = None
        ctx["story_branch_diff"] = {"head_sha": "c0ffee", "diff": diff}

    monkeypatch.setattr(pipeline_helpers, "record_story_branch_diff", record_diff)
    return {"agent_type": "claude", "story_id": "story-1", "repo_name": "live-test-level1"}


def test_a_model_s_story_branch_is_ahead_with_a_change_of_its_own(monkeypatch):
    ctx = _branch(monkeypatch, ahead_by=2, diff="diff --git a/x b/x\n+route\n")

    pipeline_helpers.record_level1_developer_path(ctx)

    assert ctx["level1_developer_commits_error"] is None
    assert ctx["level1_developer_commits"]["ahead_by"] == 2
    assert ctx["level1_developer_commits"]["diff_chars"] > 0
    # The scripted-path evidence is the noop developer's, not a model's.
    assert "level1_scripted_path" not in ctx


@pytest.mark.parametrize(
    ("ahead_by", "diff", "reason"),
    [(0, "", "not ahead of main"), (1, "  \n", "diff is empty")],
)
def test_a_model_that_committed_nothing_or_nothing_real_is_named(
    monkeypatch, ahead_by, diff, reason
):
    ctx = _branch(monkeypatch, ahead_by=ahead_by, diff=diff)

    pipeline_helpers.record_level1_developer_path(ctx)

    assert reason in ctx["level1_developer_commits_error"]
