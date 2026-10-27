"""The runner verifier refuses order evidence the native lifecycle did not produce."""

import copy
from pathlib import Path
import subprocess

import pytest

from tests.runner.verify_evidence import (
    conflict_problems,
    kit_identity_problems,
    lifecycle_problems,
    seed_causality_problems,
)

REPLIES = {
    "en": {"channel": "Enter a public channel username."},
    "ru": {"channel": "Введите имя публичного канала."},
}
OBSERVER = 424242301


def observation(language_text, channels_text, listed, after=()):
    def reply(seq, text):
        return {"seq": seq, "chat_id": OBSERVER, "text": text}

    return {
        "observer": OBSERVER,
        "grant": {"status": 200, "body": {"status": "active"}},
        "expected": {"language": "ru", "channels": ["runner_fixture"]},
        "language": {"text": "/channel", "watermark": 10, "reply": reply(11, language_text)},
        "channels": {"text": "/channels", "watermark": 11, "reply": reply(12, channels_text)},
        "module_list": {"status": 200, "body": [{"id": c, "channel": c} for c in listed]},
        "cleanup": {"removed": [], "after": {"status": 200, "body": list(after)}},
    }


def test_behaviour_caused_by_the_saved_answers_passes():
    seen = observation(REPLIES["ru"]["channel"], "@runner_fixture", ["runner_fixture"])
    assert seed_causality_problems(seen, REPLIES) == []


def test_an_ineffective_seed_fails_before_any_baseline_compensation():
    """A product that ignored both answers: default language, nobody starts subscribed."""
    seen = observation(REPLIES["en"]["channel"], "Your channel list is empty.", [])
    problems = seed_causality_problems(seen, REPLIES)
    assert len(problems) == 3
    assert "language" in problems[0] and "channels" in problems[1]


def test_a_reply_in_another_chat_or_before_the_input_is_no_observation():
    seen = observation(REPLIES["ru"]["channel"], "@runner_fixture", ["runner_fixture"])
    seen["language"]["reply"]["chat_id"] = 424242001
    seen["channels"]["reply"]["seq"] = 5
    assert len(seed_causality_problems(seen, REPLIES)) == 2


def test_an_observer_left_subscribed_is_refused():
    seen = observation(
        REPLIES["ru"]["channel"], "@runner_fixture", ["runner_fixture"], [{"id": "x"}]
    )
    assert seed_causality_problems(seen, REPLIES) == [
        "the observer kept a subscription: {'status': 200, 'body': [{'id': 'x'}]}"
    ]


PRODUCTION = {
    "project_id": "p-1",
    "story_id": "story-1",
    "repository": {"id": "repo-1"},
    "plan": {"activation": {"commit": "d" * 40}},
}


def lifecycle():
    return {
        "template": {"commit": "k" * 40, "resolved_sha": "k" * 40},
        "lifecycle": {
            "steps": [
                {
                    "kind": "full_scaffold",
                    "tick": {"entry_id": "1-0"},
                    "delivery": {"entry_id": "1-0", "result": {"status": "success"}},
                    "message": {"project_id": "p-1", "repository_id": "repo-1", "mode": "full"},
                    "project": {
                        "status": "active",
                        "workspace_ready": True,
                        "service_template": {"commit": "k" * 40},
                    },
                    "repository": {"git_url": "https://github.com/ci/runner-x"},
                    "github_calls": [{"call": "create_repo"}],
                },
                {
                    "kind": "install",
                    "package": "tg-channels",
                    "tick": {"entry_id": "3-0", "operation_id": "install-2"},
                    "delivery": {
                        "entry_id": "3-0",
                        "result": {"status": "success", "operation_id": "install-2"},
                    },
                },
            ]
        },
        "operations": {
            "tg-channels": {
                "operation_id": "install-2",
                "dispatch_entry_id": "3-0",
                "delivery_entry_id": "3-0",
                "base_sha": "r" * 40,
                "checkout": "repo-1/install-2",
            }
        },
    }


def test_native_scaffold_and_install_deliveries_pass():
    assert lifecycle_problems(lifecycle(), PRODUCTION) == []


def test_a_flag_only_scaffold_or_a_direct_install_is_refused():
    forged = lifecycle()
    forged["lifecycle"]["steps"][0]["delivery"]["entry_id"] = "9-0"
    assert lifecycle_problems(forged, PRODUCTION)
    direct = lifecycle()
    del direct["lifecycle"]["steps"][1]
    assert lifecycle_problems(direct, PRODUCTION) == ["tg-channels: 0 install deliveries"]
    missing = lifecycle()
    missing["lifecycle"]["steps"] = []
    assert lifecycle_problems(missing, PRODUCTION) == ["0 full scaffolds recorded"]


def handoff():
    glue = [
        {
            "code": code,
            "owner": "product",
            "path": path,
            "action": f"fix {code}",
        }
        for code, path in (
            ("binding_language_owner", "services/tg_bot/bindings/tg-channels.yaml"),
            ("command_collision", "services/tg_bot/src/commands.py"),
        )
    ]
    evidence = lifecycle()
    evidence["conflict_handoff"] = {
        "tg-channels": {
            "fixture_commit": "f" * 40,
            "repair_commit": "r" * 40,
            "executed": False,
            "delivery_result": {"status": "failed", "stage": "preflight"},
            "operation": {
                "id": "install-1",
                "state": "refused",
                "stage": "preflight",
                "base_sha": "f" * 40,
                "checkout": "repo-1/install-1",
                "preflight": {
                    "status": "glue",
                    "glue": glue,
                    "target": {"catalog_ref": "d" * 40},
                },
            },
            "task_after": {"install_operation": None, "blocked_by_task_id": "task-fix"},
            "repair_task": {
                "id": "task-fix",
                "type": "fix",
                "created_by": "catalog_install_glue",
                "dispatch_admitted": True,
                "story_id": "story-1",
                "description": "\n".join(item["action"] for item in glue),
            },
            "redelivery": {"result": {"status": "skipped"}},
        }
    }
    return evidence


def test_a_kit_classified_conflict_handed_to_one_repair_passes():
    assert conflict_problems(handoff(), PRODUCTION, ["tg-channels"]) == []


def test_an_opaque_or_unrepaired_conflict_is_refused():
    for change in (
        lambda record: record["operation"]["preflight"]["glue"].pop(0),
        lambda record: record["redelivery"]["result"].update(status="success"),
        lambda record: record.update(repair_commit="0" * 40),
        lambda record: record["operation"]["preflight"]["glue"][1].update(owner="package:x"),
    ):
        evidence = copy.deepcopy(handoff())
        change(evidence["conflict_handoff"]["tg-channels"])
        assert conflict_problems(evidence, PRODUCTION, ["tg-channels"]), change
    assert conflict_problems(lifecycle(), PRODUCTION, ["tg-channels"]) == [
        "no conflict handoff for tg-channels"
    ]


@pytest.fixture
def kit_history(tmp_path: Path) -> tuple[Path, str, str]:
    """A kit repository whose main holds a tooling commit and a later catalog commit."""

    def git(*argv: str) -> str:
        return subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *argv],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "tooling release")
    tooling = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "catalog lists a new release")
    catalog = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", catalog)
    return tmp_path, tooling, catalog


def identity_evidence(harness: str, template: str, planner: str, product: str) -> dict:
    return {
        "pinned": {"kit": harness},
        "template": {"resolved_sha": template},
        "revisions": {
            "planner_tooling": {"vcs_info": {"commit_id": planner}},
            "product_tooling": {"vcs_info": {"commit_id": product}},
        },
    }


@pytest.mark.subprocess
def test_the_tooling_release_over_a_later_catalog_commit_passes(kit_history):
    kit, tooling, catalog = kit_history
    activation = {"commit": catalog, "tooling_commit": tooling}
    evidence = identity_evidence(catalog, tooling, tooling, tooling)

    assert kit_identity_problems(evidence, activation, kit) == []


@pytest.mark.subprocess
def test_a_render_off_the_tooling_or_a_catalog_older_than_its_tooling_is_refused(kit_history):
    kit, tooling, catalog = kit_history
    activation = {"commit": catalog, "tooling_commit": tooling}
    rendered_at_catalog = identity_evidence(catalog, catalog, tooling, tooling)
    predating = {"commit": tooling, "tooling_commit": catalog}

    assert kit_identity_problems(rendered_at_catalog, activation, kit) == [
        f"the template render is {catalog}, not the tooling commit {tooling}"
    ]
    [problem] = kit_identity_problems(
        identity_evidence(tooling, catalog, catalog, catalog), predating, kit
    )
    assert problem.startswith(f"{tooling} does not descend from {catalog}")
