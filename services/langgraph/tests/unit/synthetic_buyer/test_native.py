"""Judging the platform's own records: an absent fact is unknown, a contradiction failed."""

from __future__ import annotations

from dataclasses import replace

from src.synthetic_buyer import native, probe
from src.synthetic_buyer.evidence import ObservationStatus
from src.synthetic_buyer.repository_evidence import (
    CompareFacts,
    PullRequestFacts,
    WorkflowRunFacts,
)
from src.synthetic_buyer.telegram import Message
from tests.unit.synthetic_buyer.fakes import (
    INSTALL_HEAD,
    MERGE,
    PR_HEAD,
    PROJECT,
    SCAFFOLD,
    START,
    install,
    install_operation,
    plan,
)

STORY = "story-1"
DEPLOY = {
    "id": "dep-1",
    "type": "deploy",
    "status": "completed",
    "project_id": PROJECT,
    "story_id": STORY,
    "completed_at": "2026-10-10T13:00:00+00:00",
    "result": {
        "deploy_outcome": "success",
        "deployed_url": "https://product.example.test",
        "bot_username": "channels_digest_bot",
        "application_id": 5,
    },
}


def _qa(completed_at: str, outcome: str = "passed", url: str = "https://product.example.test"):
    return {
        "id": "qa-1",
        "type": "qa",
        "status": "completed",
        "project_id": PROJECT,
        "story_id": STORY,
        "completed_at": completed_at,
        "result": {"qa_outcome": outcome, "deployed_url": url},
    }


def test_a_from_scratch_plan_is_not_a_module_order():
    assert native.plan_routes(plan("module")).status is ObservationStatus.OBSERVED
    assert native.plan_routes(plan("from_scratch")).status is ObservationStatus.FAILED
    assert native.plan_routes(None).status is ObservationStatus.FAILED


def test_a_preview_made_before_the_rollout_contradicts_the_order():
    preview = {"created_at": "2026-10-10T12:00:00+00:00", "project_id": PROJECT}

    assert native.preview_after_allowlist(preview, "2026-10-10T12:05:00+00:00", PROJECT).status is (
        ObservationStatus.FAILED
    )
    assert native.preview_after_allowlist(preview, None, PROJECT).status is (
        ObservationStatus.UNKNOWN
    )
    assert native.preview_after_allowlist(
        preview | {"created_at": "2026-10-10T12:06:00+00:00"}, "2026-10-10T12:05:00+00:00", "other"
    ).status is (ObservationStatus.FAILED)


def _install_task(**operation) -> dict:
    return {
        "id": "i",
        "type": "install",
        "status": "done",
        "install": install(),
        "install_operation": install_operation(**operation),
    }


PULL = PullRequestFacts(1, True, PR_HEAD, SCAFFOLD, MERGE)
PUBLISHED = {"i": CompareFacts(SCAFFOLD, INSTALL_HEAD, "ahead", ("c1",), ("pyproject.toml",))}
AFTER = CompareFacts(INSTALL_HEAD, PR_HEAD, "identical", (), ())


def test_a_dependency_path_or_published_status_alone_proves_no_mechanical_install():
    bare = {
        "id": "i",
        "type": "install",
        "status": "done",
        "install_operation": {"state": "published"},
    }

    finding, _ = native.install_chain([bare], [], PULL, {}, AFTER)

    assert finding.status is ObservationStatus.UNKNOWN


def test_the_install_chain_starts_on_the_scaffold_and_the_story_contains_it():
    observed, head = native.install_chain([_install_task()], [], PULL, PUBLISHED, AFTER)
    elsewhere, _ = native.install_chain(
        [_install_task(base_sha="8" * 40)], [], PULL, PUBLISHED, AFTER
    )
    modelled, _ = native.install_chain(
        [_install_task()], [{"id": "e", "task_id": "i"}], PULL, PUBLISHED, AFTER
    )
    dropped, _ = native.install_chain(
        [_install_task()], [], PULL, PUBLISHED, replace(AFTER, status="diverged")
    )

    assert (observed.status, head) == (ObservationStatus.OBSERVED, INSTALL_HEAD)
    assert {elsewhere.status, modelled.status, dropped.status} == {ObservationStatus.FAILED}


def test_engineering_after_the_install_is_judged_by_the_files_it_changed():
    tasks = [_install_task()]
    feature = replace(AFTER, status="ahead", commits=("g",), files=("services/x.py",))

    assert native.glue_only(tasks, plan("module"), AFTER).status is ObservationStatus.OBSERVED
    assert native.glue_only(tasks, plan("module"), feature).status is ObservationStatus.FAILED
    assert native.glue_only(tasks, plan("module_with_glue"), feature).status is (
        ObservationStatus.UNKNOWN
    )
    assert native.glue_only(tasks, plan("module"), None).status is ObservationStatus.UNKNOWN


def _deploy_run(**placed) -> dict:
    deployment = {
        "status": "success",
        "run_id": 9001,
        "deployed_commit_sha": MERGE,
        "image_references": {"BACKEND_IMAGE": "registry/p:x"},
        "image_digests": {"BACKEND_IMAGE": "sha256:" + "c" * 64},
    } | placed
    return DEPLOY | {"result": DEPLOY["result"] | {"deployment_result": deployment}}


STORY_RECORD = {"generated_product_timeline": {"pull_request": {"head_sha": PR_HEAD}}}
PUBLICATION = WorkflowRunFacts(9001, MERGE, "completed", "success", "main.yml")


def test_a_typed_success_without_its_provenance_chain_is_not_an_observed_deploy():
    _, bare = native.deploy_typed(PROJECT, STORY, [DEPLOY])
    _, full = native.deploy_typed(PROJECT, STORY, [_deploy_run()])
    _, undigested = native.deploy_typed(PROJECT, STORY, [_deploy_run(image_digests={})])

    assert native.deploy_provenance(bare, STORY_RECORD, PULL, PUBLICATION).status is (
        ObservationStatus.UNKNOWN
    )
    assert native.deploy_provenance(undigested, STORY_RECORD, PULL, PUBLICATION).status is (
        ObservationStatus.UNKNOWN
    )
    assert native.deploy_provenance(full, STORY_RECORD, PULL, None).status is (
        ObservationStatus.UNKNOWN
    )
    assert native.deploy_provenance(full, STORY_RECORD, PULL, PUBLICATION).status is (
        ObservationStatus.OBSERVED
    )


def test_inconsistent_deploy_provenance_fails():
    _, full = native.deploy_typed(PROJECT, STORY, [_deploy_run()])
    other_head = {"generated_product_timeline": {"pull_request": {"head_sha": "7" * 40}}}

    for story, pull, publication in (
        (other_head, PULL, PUBLICATION),
        (STORY_RECORD, PULL, replace(PUBLICATION, conclusion="failure")),
        (STORY_RECORD, PULL, replace(PUBLICATION, head_sha="9" * 40)),
        (STORY_RECORD, replace(PULL, merge_commit_sha="6" * 40), PUBLICATION),
    ):
        assert native.deploy_provenance(full, story, pull, publication).status is (
            ObservationStatus.FAILED
        )


def test_qa_binds_to_the_storys_deploy_or_does_not_count():
    finding, deploy = native.deploy_typed(PROJECT, STORY, [DEPLOY])
    assert finding.status is ObservationStatus.OBSERVED

    after = native.qa_passed(PROJECT, STORY, [_qa("2026-10-10T13:10:00+00:00")], deploy)
    before = native.qa_passed(PROJECT, STORY, [_qa("2026-10-10T12:50:00+00:00")], deploy)
    elsewhere = native.qa_passed(
        PROJECT, STORY, [_qa("2026-10-10T13:10:00+00:00", url="https://other.test")], deploy
    )
    red = native.qa_passed(PROJECT, STORY, [_qa("2026-10-10T13:10:00+00:00", "failed")], deploy)

    assert after.status is ObservationStatus.OBSERVED
    assert {before.status, elsewhere.status, red.status} == {ObservationStatus.FAILED}
    assert native.qa_passed(PROJECT, STORY, [], deploy).status is ObservationStatus.UNKNOWN


def test_product_ci_needs_a_merged_pr_and_a_green_run_on_its_head():
    timeline = {
        "pull_request": {"merged_at": "2026-10-10T12:30:00+00:00", "head_sha": "a" * 40},
        "ci_runs": [{"id": 1, "conclusion": "failure", "head_sha": "a" * 40}],
    }

    assert native.product_ci({"generated_product_timeline": timeline}).status is (
        ObservationStatus.UNKNOWN
    )
    timeline["ci_runs"].append({"id": 2, "conclusion": "success", "head_sha": "a" * 40})
    assert native.product_ci({"generated_product_timeline": timeline}).status is (
        ObservationStatus.OBSERVED
    )


def _message(text: str, *urls: str) -> Message:
    return Message(id=1, sender_id=2, outgoing=False, date=START, text=text, urls=urls)


def test_product_language_and_links_are_read_from_the_bots_own_words():
    assert probe.answered_in_russian([_message("Список каналов пуст.")])
    assert not probe.answered_in_russian(
        [_message("Настройте язык продукта (ru/en) через /settings/set.")]
    )
    assert probe.answered_in_english([_message("Your channel list is empty.")])
    assert not probe.answered_in_english([_message("Channel @x"), _message("Канал")])
    linked = _message("post", "https://t.me/Chan_One/15", "https://t.me/elsewhere/3")
    assert probe.channel_post_links(linked, ["chan_one"]) == [
        ("chan_one", 15, "https://t.me/Chan_One/15")
    ]
    assert probe.missing_channels([_message("@chan_one")], ["chan_one", "chan_two"]) == ["chan_two"]
