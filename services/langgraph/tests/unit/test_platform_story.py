"""Native third-story orchestration preserves prior scopes and retains partial evidence."""

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def story(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/live"))
    import mechanical_platform

    return mechanical_platform


@pytest.mark.parametrize("refused", [False, True])
async def test_third_install_owns_claim_then_deploys_qa_and_revokes_without_engineering(  # noqa: PLR0915 - full lifecycle and both scope exits
    monkeypatch, tmp_path, story, refused
):
    h, m = story.h, story.m
    now = datetime.now(UTC).isoformat()
    fixture = json.loads(story.DATA.read_text())
    fixture["release"] = {
        "version": "1.0",
        "tag": "opaque-release",
        "binding_sha256": "binding",
        "manifest_sha256": "manifest",
    }
    data = tmp_path / "conversation.json"
    data.write_text(json.dumps(fixture))
    monkeypatch.setattr(story, "DATA", data)
    install = {
        "package": {
            "name": "opaque-module",
            "distribution": "opaque-distribution",
            "version": "1.0",
            "tag": "opaque-release",
        },
        "libraries": [],
        "binding": {"resource": "opaque_module:bindings/default.yaml", "sha256": "binding"},
        "tooling_commit": "tooling",
    }
    task = {
        "id": "third-task",
        "type": "install",
        "dispatch_admitted": True,
        "planning_attempt_id": "third-plan",
        "install": install,
        "status": "done",
        "install_operation": {
            "id": "operation",
            "state": "published",
            "base_sha": "reminders-merge",
            "head_sha": "third-head",
            "token": "private-token",
            "verification": {"binding_sha256": "binding", "tooling_commit": "tooling"},
        },
    }
    previous = {
        "story_id": "reminders-story",
        "qa_run": {"id": "reminders-qa"},
        "old-only": "prior-story",
    }
    ctx = {
        "project_id": "product",
        "project_name": "stand-product",
        "server_handle": "target",
        "repo_name": "repo",
        "story_id": "notes-story",
        "task_ids": ["note-1", "note-2"],
        "level1_marker": "marker",
        "level1_extension": previous,
        h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY: ["note-1", "note-2"],
        "mechanical_acceptance": {
            "status": "passed",
            "phase": "completed",
            "readback": {"publication": {"merge_sha": "reminders-merge"}},
        },
    }
    expected = deepcopy(ctx)
    api = SimpleNamespace(get=AsyncMock())

    def response(value):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: deepcopy(value))

    async def get(path):
        if path.startswith("/api/stories/"):
            return response({"created_at": now, "pr_number": 4})
        if path.startswith("/api/tasks/?"):
            return response([task])
        if path.startswith("/api/tasks/"):
            value = deepcopy(task)
            if refused:
                value["install_operation"]["state"] = "refused"
            return response(value)
        if path.endswith("/coverage"):
            return response({"covered": True})
        raise AssertionError(path)

    api.get.side_effect = get

    async def create(api, ctx):
        assert ctx["level1_brief"].settings_key == "language"
        assert ctx["level1_brief"].settings_value == "ru"
        ctx.update(
            story_id="third-story",
            brief_id="third-brief",
            brief_read={"confirmed_at": now},
            level1_planning_attempt_id="third-plan",
        )

    monkeypatch.setattr(h, "create_level1_confirmed_brief", create)
    monkeypatch.setattr(h, "_write_level1_qa_criteria", AsyncMock())
    monkeypatch.setattr(h, "verify_level1_plan_is_this_runs_alone", AsyncMock())
    commands = []

    def invoke(service, module, args, **kwargs):
        commands.append((service, module, args))
        value = (
            {"coverage_outcome": "admitted"}
            if module.endswith("scripted_install_plan")
            else {
                "issuance": {"key_ids": ["safe-key"]},
                "deployment": {"backend": {"component": {"manifest_sha256": "manifest"}}},
            }
        )
        event = (
            "scripted_install_result"
            if module.endswith("scripted_install_plan")
            else "platform_readback"
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps({"event": event, "result": value}))

    monkeypatch.setattr(h, "docker_exec_python_module", invoke)
    monkeypatch.setattr(
        m, "sql_snapshot", Mock(return_value={"project_ledger": [{"role": "engineering"}]})
    )
    monkeypatch.setattr(m, "execution_readback", Mock(return_value=["native-stages"]))

    async def deploy(api, ctx, **kwargs):
        ctx.update(
            deploy_run_id="third-deploy",
            deploy_run_record={},
            deploy_merge_commit_sha="third-merge",
            deployed_image_references={},
            story_ci_runs=[],
            level1_merge_artifact={},
            deploy_head_sha="third-head",
        )
        return {"id": "third-deploy"}

    monkeypatch.setattr(h, "wait_brief_deploy_run", deploy)

    async def outcome(api, ctx, **kwargs):
        ctx["deploy_outcome"] = "success"
        return {"status": "completed"}

    monkeypatch.setattr(h, "wait_deploy_outcome", outcome)
    monkeypatch.setattr(h, "wait_deploy", AsyncMock())
    monkeypatch.setattr(h, "record_story_ci_runs", AsyncMock())

    def images(ctx):
        ctx.update(deployed_commit_sha="third-merge", main_head_probe={"sha": "third-merge"})
        return True

    monkeypatch.setattr(h, "record_deployed_image_tags", images)
    checked = Mock()
    monkeypatch.setattr(story, "check_readback", checked)
    qa = AsyncMock(return_value=True)

    async def run_qa(api, story_id, **kwargs):
        await qa(story_id)
        probe = {
            "status": "passed",
            "phase": "completed",
            "languages": {"ru": True, "en": True},
            "grant": {"head_sha": "third-head", "application_id": 3},
        }
        ctx["application_id"] = 3
        ctx["qa_run"] = {
            "id": "third-qa",
            "status": "completed",
            "result": {"qa_outcome": "passed", "report": json.dumps(probe)},
        }
        return {"passed": True}

    monkeypatch.setattr(h, "run_non_llm_qa", run_qa)
    monkeypatch.setattr(h, "record_qa_settlement_evidence", AsyncMock())
    monkeypatch.setattr(h, "wait_story_completed", AsyncMock(return_value={"status": "completed"}))
    revoke = AsyncMock(return_value={"status": "revoked"})
    monkeypatch.setattr(m, "revoke_readback", revoke)
    monkeypatch.setattr(m, "model_observation", Mock(return_value={"events": {}}))
    monkeypatch.setitem(
        sys.modules,
        "test_full_pipeline",
        SimpleNamespace(
            _level1_extension_owner_told=AsyncMock(), _require_level1_merge_artifact=Mock()
        ),
    )
    if refused:
        with pytest.raises(h.Level1PhaseFailed, match="refused"):
            await story.native_third_story(api, api, api, ctx, debug_prefix="unit")
        qa.assert_not_awaited()
        assert ctx["mechanical_acceptance"]["platform_story"]["phase"] == "install"
    else:
        await story.native_third_story(api, api, api, ctx, debug_prefix="unit")
        qa.assert_awaited_once_with("third-story")
        revoke.assert_awaited_once()
        assert ctx["mechanical_acceptance"]["platform_story"]["status"] == "passed"
        assert "--capability" in commands[0][2]
        assert commands[1][1] == "shared.live_harness_platform"
        checked.assert_called_once()
    assert ctx["story_id"] == expected["story_id"]
    assert ctx["level1_extension"] == previous
    assert (
        ctx[h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY]
        == expected[h.ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY]
    )
    assert ctx["level1_platform_extension"]["story_id"] == "third-story"
    assert "old-only" not in ctx["level1_platform_extension"]


def readback_facts():
    package = {"name": "opaque-module", "version": "1.0"}
    task = {"install": {"package": package, "binding": {"sha256": "binding"}}}
    operation = {"base_sha": "previous-merge", "head_sha": "third-head"}
    publication = {
        "owned_hashes": {"notes.py": "notes"},
        "merge_sha": "third-merge",
        "merge_base": "previous-merge",
        "pull_request": {
            "merged": True,
            "head": {"sha": "third-head"},
            "merge_commit_sha": "third-merge",
        },
        "merge_ci": [
            {
                "head_sha": "third-merge",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "status": "completed",
            }
        ],
    }
    refs = {"BACKEND_IMAGE": "registry/backend:third", "TG_BOT_IMAGE": "registry/bot:third"}
    deployment = {
        service: {
            "reference": refs[f"{service.upper()}_IMAGE"],
            "registry_digest": "sha256:" + "a" * 64,
            "digests": ["registry/image@sha256:" + "a" * 64],
            "distributions": {"prior-component": "1.0"},
            "hashes": {"notes.py": "notes"},
            "core": "2.4",
            "component": {
                "version": "1.0",
                "active": {**package, "manifest_sha256": "manifest"},
                "manifest_sha256": "manifest",
                "binding_sha256": "binding",
            },
        }
        for service in ("backend", "tg_bot")
    }
    facts = {"publication": publication, "deployment": deployment}
    baseline = deepcopy(facts)
    return facts, baseline, task, operation, refs


def test_third_readback_proves_current_images_activation_and_preserved_sources(story):
    story.check_readback(*readback_facts())


@pytest.mark.parametrize(
    "change", ["source", "image", "ci", "pr", "activation", "core", "binding", "distribution"]
)
def test_third_readback_refuses_mismatched_deployment(story, change):
    facts, baseline, task, operation, refs = readback_facts()
    deployed = facts["deployment"]["backend"]
    if change == "source":
        facts["publication"]["owned_hashes"]["notes.py"] = "changed"
    elif change == "image":
        deployed["registry_digest"] = "sha256:" + "b" * 64
    elif change == "ci":
        facts["publication"]["merge_ci"][0]["head_sha"] = "other-merge"
    elif change == "pr":
        facts["publication"]["pull_request"]["head"]["sha"] = "other-head"
    elif change == "activation":
        deployed["component"]["active"]["manifest_sha256"] = "other-manifest"
    elif change == "core":
        deployed["core"] = "2.1"
    elif change == "binding":
        facts["deployment"]["tg_bot"]["component"]["binding_sha256"] = "other-binding"
    else:
        deployed["distributions"]["prior-component"] = "2.0"
    with pytest.raises(story.h.Level1PhaseFailed):
        story.check_readback(facts, baseline, task, operation, refs)
