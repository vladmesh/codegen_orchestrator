"""Offline regressions for the level-1 story-merge artifact predicate."""

from __future__ import annotations

import json
import subprocess

from level1_merge_artifact import change_set_comparison, merge_artifact_mismatches
import pipeline_helpers
import pytest
import run_evidence
from worker_wrapper.injected_paths import INJECTED_PATHS

pytestmark = pytest.mark.needs_no_api_credential

EXPECTED_PATHS = [
    "services/backend/manifest.yaml",
    "services/backend/src/app/api/router.py",
    "services/backend/src/app/api/routers/level1.py",
]
PRODUCT_AGENTS = "# Product instructions\n\nKeep changes product-scoped.\n"


def _merge(
    paths: list[str] | None = None,
    *,
    makefile: str | None = None,
    agents: str | None = None,
    merged_into_default_branch: bool = True,
) -> dict:
    paths = list(EXPECTED_PATHS if paths is None else paths)
    files = {}
    if makefile is not None:
        files["Makefile"] = makefile
    if agents is not None:
        files["AGENTS.md"] = agents
    return {
        "merge_commit_sha": "a" * 40,
        "default_branch": "main",
        "merged_into_default_branch": merged_into_default_branch,
        "changed_paths": paths,
        "file_contents": files,
    }


def _reasons(observation: dict | None) -> list[str]:
    return merge_artifact_mismatches(
        observation,
        product_agents_content=PRODUCT_AGENTS,
    )


def test_a_clean_merge_matches_its_declared_level1_change_set():
    assert _reasons(_merge()) == []
    assert change_set_comparison(_merge(), expected_paths=EXPECTED_PATHS) == {
        "exact_match": True,
        "extra_paths": [],
        "missing_declared_paths": [],
    }


def test_an_extra_product_path_is_recorded_without_failing_the_artifact_verdict():
    observation = _merge([*EXPECTED_PATHS, "services/backend/src/generated/settings.py"])

    assert _reasons(observation) == []
    assert change_set_comparison(observation, expected_paths=EXPECTED_PATHS) == {
        "exact_match": False,
        "extra_paths": ["services/backend/src/generated/settings.py"],
        "missing_declared_paths": [],
    }


@pytest.mark.parametrize("injected", INJECTED_PATHS)
def test_every_injected_path_in_a_merge_is_named(injected: str):
    path = injected.rstrip("/")
    reasons = _reasons(_merge([*EXPECTED_PATHS, path]))

    assert any(path in reason and "injected path" in reason for reason in reasons)


def test_a_merge_with_the_old_compose_proxy_makefile_block_is_named():
    reasons = _reasons(
        _merge(
            [*EXPECTED_PATHS, "Makefile"],
            makefile="all:\n\t@true\n# --- orchestrator overrides ---\n",
        )
    )

    assert any("orchestrator overrides" in reason for reason in reasons)


def test_a_merge_that_overwrites_product_agents_with_orchestrator_instructions_is_named():
    reasons = _reasons(
        _merge(
            [*EXPECTED_PATHS, "AGENTS.md"],
            agents="# Developer instructions\n\nRead TASK.md and complete the task.\n",
        )
    )

    assert any("AGENTS.md" in reason and "product instructions" in reason for reason in reasons)


def test_an_agents_overwrite_without_its_pre_merge_product_content_is_not_a_pass():
    observation = _merge(
        [*EXPECTED_PATHS, "AGENTS.md"],
        agents="# Developer instructions\n\nRead TASK.md and complete the task.\n",
    )

    reasons = merge_artifact_mismatches(observation, product_agents_content=None)

    assert "the product AGENTS.md content before the merge was not captured" in reasons


def test_a_missing_merge_capture_is_a_failure_not_a_pass():
    assert _reasons(None) == ["the story merge file set was never captured"]


def test_the_artifact_carries_each_storys_observation_and_verdict():
    first = _merge()
    second = _merge(["services/backend/src/app/api/routers/level1_extension.py"])
    ctx = {
        "level1_merge_artifact": first,
        "level1_merge_artifact_verdict": {"holds": True, "reasons": []},
        "level1_extension": {
            "level1_merge_artifact": second,
            "level1_merge_artifact_verdict": {"holds": False, "reasons": ["unexpected path"]},
        },
    }

    evidence = run_evidence.story_merge_artifact_evidence(ctx)

    assert evidence["first_story"]["observation"]["value"] == first
    assert evidence["first_story"]["verdict"]["value"] == {"holds": True, "reasons": []}
    assert evidence["second_story"]["observation"]["value"] == second
    assert evidence["second_story"]["verdict"]["value"]["reasons"] == ["unexpected path"]


def test_the_harness_records_and_judges_the_merge_before_teardown(monkeypatch):
    observation = _merge()
    monkeypatch.setattr(pipeline_helpers, "probe_merge_file_set", lambda *_args: observation)
    ctx = {
        "repo_name": "run-repo",
        "deploy_merge_commit_sha": "a" * 40,
        "level1_change_set_paths": EXPECTED_PATHS,
    }

    assert pipeline_helpers.record_level1_merge_artifact(ctx) is True
    assert ctx["level1_merge_artifact"] == observation
    assert ctx["level1_merge_artifact_verdict"] == {
        "holds": True,
        "reasons": [],
        "change_set": {
            "exact_match": True,
            "extra_paths": [],
            "missing_declared_paths": [],
        },
    }


def test_the_harness_reads_the_merge_set_through_the_github_app_probe(monkeypatch):
    observation = _merge()
    calls = []

    def probe(service, module, args, timeout=30):
        calls.append((service, module, args, timeout))
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=pipeline_helpers.MERGE_FILE_SET_PROBE_MARKER + json.dumps(observation),
            stderr="",
        )

    monkeypatch.setattr(pipeline_helpers, "docker_exec_python_module", probe)

    assert pipeline_helpers.probe_merge_file_set("run-repo", "a" * 40) == observation
    assert calls[0][:2] == ("langgraph", "shared.live_harness_cleanup")
    assert calls[0][2][0] == "merge-file-set-probe"


def test_an_unreadable_merge_capture_leaves_a_red_verdict(monkeypatch):
    def unreadable(*_args):
        raise RuntimeError("GitHub 503")

    monkeypatch.setattr(pipeline_helpers, "probe_merge_file_set", unreadable)
    ctx = {
        "repo_name": "run-repo",
        "deploy_merge_commit_sha": "a" * 40,
        "level1_change_set_paths": EXPECTED_PATHS,
    }

    assert pipeline_helpers.record_level1_merge_artifact(ctx) is False
    assert ctx["level1_merge_artifact_verdict"]["holds"] is False
    assert "GitHub 503" in ctx["level1_merge_artifact_verdict"]["reasons"][0]
