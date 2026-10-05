"""Offline rejection checks for the final native stand evidence."""

import tomllib
from types import SimpleNamespace

from level1_change_set import _fixture_text
from mechanical_install import check_execution, check_readback, command_result, install_scope
from mechanical_notes import notes_operations
from pipeline_helpers import ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY, Level1PhaseFailed
import pytest

pytestmark = pytest.mark.needs_no_api_credential


def test_native_task_does_not_enter_the_first_story_engineering_roster():
    ctx = {"task_ids": ["notes-backend", "notes-bot"], "mechanical_acceptance": {}}
    with install_scope(ctx):
        ctx["task_ids"] = ["native-install"]
    assert ctx[ENGINEERING_ATTEMPT_TASK_IDS_CTX_KEY] == ["notes-backend", "notes-bot"]
    assert ctx["level1_extension"]["task_ids"] == ["native-install"]


def test_notes_reuses_the_released_dependencies_and_registers_owned_handlers():
    backend, bot = notes_operations("unique")
    pyproject = next(
        one.content for one in backend if one.path == "services/backend/pyproject.toml"
    )
    released = tomllib.loads(_fixture_text("services/backend/pyproject.toml"))
    assert (
        tomllib.loads(pyproject)["project"]["dependencies"] == released["project"]["dependencies"]
    )
    main = next(one.content for one in bot if one.path == "services/tg_bot/src/main.py")
    assert 'CommandHandler("note", handle_note)' in main
    assert 'CommandHandler("notes", handle_notes)' in main
    assert "bindings.register(application, BackendClient)" in main


def test_native_git_proof_refuses_missing_stage_hooks_or_force():
    stages = [
        {"stage": stage, "argv": ["kit", "validate"], "returncode": 0}
        for stage in (
            "preflight",
            "package",
            "library",
            "bind",
            "generate",
            "validate",
            "readback",
            "commit",
            "push",
        )
    ]
    check_execution(stages)
    stages[-1]["argv"] = ["git", "push", "origin"]
    with pytest.raises(Level1PhaseFailed, match="hooks"):
        check_execution(stages)
    stages[-1]["argv"] = ["git", "-c", "core.hooksPath=/dev/null", "push", "--force"]
    with pytest.raises(Level1PhaseFailed, match="forced"):
        check_execution(stages)
    with pytest.raises(Level1PhaseFailed, match="every required stage"):
        check_execution(stages[:-1])


def baseline():
    files = {
        "services/backend/src/app/api/routers/notes.py": "a" * 64,
        "services/backend/src/app/api/router.py": "b" * 64,
        "services/tg_bot/src/handlers/notes.py": "c" * 64,
        "services/tg_bot/src/main.py": "d" * 64,
    }
    return {
        "publication": {
            "head": "a" * 40,
            "owned_hashes": files,
            "merge_sha": "a" * 40,
            "merge_ci": [
                {
                    "id": 7,
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                    "path": ".github/workflows/ci.yml",
                }
            ],
        },
        "deployment": {
            service: {
                "core": "2.2.0",
                "digests": [f"registry/{service}@sha256:" + "a" * 64],
                "reference": f"registry/{service}:sha-{'a' * 12}",
                "registry_digest": "sha256:" + "a" * 64,
                "distributions": {"codegen-kit-reminders": None, "codegen-kit-textparse": None},
                "hashes": {
                    path: digest for path, digest in files.items() if path.split("/")[1] == service
                },
            }
            for service in ("backend", "tg_bot")
        },
    }


def test_before_install_requires_no_components_in_either_service():
    facts = baseline()
    check_readback(facts, installed=False)
    facts["deployment"]["tg_bot"]["distributions"]["codegen-kit-reminders"] = "0.5.0"
    with pytest.raises(Level1PhaseFailed, match="readback"):
        check_readback(facts, installed=False)


def test_running_digest_must_match_the_published_commit_image():
    facts = baseline()
    facts["deployment"]["backend"]["registry_digest"] = "sha256:" + "b" * 64
    with pytest.raises(Level1PhaseFailed, match="readback"):
        check_readback(facts, installed=False)


def test_readback_uses_the_native_deploy_environment_image_keys():
    facts = baseline()
    references = {
        f"{service.upper()}_IMAGE": values["reference"]
        for service, values in facts["deployment"].items()
    }
    check_readback(facts, installed=False, expected_references=references)
    references["TG_BOT_IMAGE"] = "registry/tg_bot:wrong-commit"
    with pytest.raises(Level1PhaseFailed, match="readback"):
        check_readback(facts, installed=False, expected_references=references)


def test_deployed_publication_requires_green_ci_for_the_actual_merge():
    facts = baseline()
    facts["publication"]["merge_ci"][0]["conclusion"] = "failure"
    with pytest.raises(Level1PhaseFailed, match="publication"):
        check_readback(facts, installed=False)


@pytest.mark.parametrize("change", ["protected", "binding", "components", "base", "pr"])
def test_installed_evidence_rejects_wrong_protection_binding_releases_base_or_pr(change):
    before = baseline()
    facts = baseline()
    operation = {
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "verification": {
            "binding_sha256": "e" * 64,
            "component_targets": {
                "reminders": "2748ffd05a982b9d193f4e43d47f6e4a6ff70a21",
                "textparse": "d68d997fe43a6067bfadf25bd76283141e1fc598",
            },
            "protected_sha256": dict(before["publication"]["owned_hashes"]),
        },
    }
    facts["deployment"]["backend"]["distributions"]["codegen-kit-reminders"] = "0.5.0"
    facts["deployment"]["tg_bot"]["distributions"]["codegen-kit-textparse"] = "0.1.0"
    facts["deployment"]["tg_bot"]["hashes"]["services/tg_bot/bindings/reminders.yaml"] = "e" * 64
    facts["publication"].update(
        head="b" * 40,
        merge_base="a" * 40,
        pull_request={"merged": True, "merge_commit_sha": "a" * 40, "head": {"sha": "b" * 40}},
    )
    check_readback(facts, installed=True, baseline=before, operation=operation)
    if change == "protected":
        operation["verification"]["protected_sha256"].clear()
    elif change == "binding":
        operation["verification"]["binding_sha256"] = "f" * 64
    elif change == "components":
        operation["verification"]["component_targets"]["textparse"] = "foreign"
    elif change == "base":
        operation["base_sha"] = "d" * 40
    else:
        facts["publication"]["pull_request"]["head"]["sha"] = "foreign"
    with pytest.raises(Level1PhaseFailed):
        check_readback(facts, installed=True, baseline=before, operation=operation)


@pytest.mark.parametrize("change", ["digest", "core", "notes", "binding"])
def test_baseline_refuses_missing_digest_wrong_core_lost_notes_or_existing_binding(change):
    facts = baseline()
    if change == "digest":
        facts["deployment"]["backend"]["digests"] = []
    elif change == "core":
        facts["deployment"]["backend"]["core"] = "2.1.0"
    elif change == "notes":
        facts["deployment"]["tg_bot"]["hashes"]["services/tg_bot/src/handlers/notes.py"] = "foreign"
    else:
        facts["deployment"]["tg_bot"]["hashes"]["services/tg_bot/bindings/reminders.yaml"] = (
            "a" * 64
        )
    with pytest.raises(Level1PhaseFailed):
        check_readback(facts, installed=False)


@pytest.mark.parametrize(
    "stdout,rc",
    [
        ("", 0),
        ('{"event":"scripted_install_result","result":{}}', 1),
        ('{"event":"foreign","result":{}}', 0),
    ],
)
def test_ambiguous_invocation_is_never_accepted(stdout, rc):
    with pytest.raises(Level1PhaseFailed, match="invocation"):
        command_result(SimpleNamespace(stdout=stdout, returncode=rc), "scripted_install_result")
