"""Judging the platform's own records: an absent fact is unknown, a contradiction failed."""

from __future__ import annotations

from src.synthetic_buyer import native, probe
from src.synthetic_buyer.evidence import ObservationStatus
from src.synthetic_buyer.telegram import Message
from tests.unit.synthetic_buyer.fakes import PROJECT, START, plan

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
    preview = {"created_at": "2026-10-10T12:00:00+00:00"}

    assert native.preview_after_allowlist(preview, "2026-10-10T12:05:00+00:00").status is (
        ObservationStatus.FAILED
    )
    assert native.preview_after_allowlist(preview, None).status is ObservationStatus.UNKNOWN


def test_engineering_that_does_not_wait_on_the_install_is_not_glue():
    tasks = [
        {"id": "i", "type": "install"},
        {"id": "g", "type": "feature", "blocked_by_task_id": "i"},
        {"id": "g2", "type": "fix", "blocked_by_task_id": "g"},
        {"id": "s", "type": "feature", "blocked_by_task_id": None},
    ]

    finding = native.glue_only(tasks)

    assert finding.status is ObservationStatus.FAILED
    assert finding.detail["not_glue"] == ["s"]


def test_an_install_worked_by_a_model_is_not_mechanical():
    tasks = [
        {
            "id": "i",
            "type": "install",
            "status": "done",
            "install_operation": {"state": "published"},
        }
    ]

    assert native.install_mechanical(tasks, []).status is ObservationStatus.OBSERVED
    assert native.install_mechanical(tasks, [{"id": "e", "task_id": "i"}]).status is (
        ObservationStatus.FAILED
    )
    assert native.install_mechanical([], []).status is ObservationStatus.FAILED


def test_qa_binds_to_the_storys_deploy_or_does_not_count():
    finding, deploy = native.deploy_success(PROJECT, STORY, [DEPLOY])
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
