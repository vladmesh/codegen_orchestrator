"""Ordinary terminal cleanup preserves acceptance before destroying owned records."""

import json

import pytest

from tests.unit.synthetic_buyer.fakes import BUYER, PROJECT, UNRELATED_PROJECT, harness


async def test_owned_delete_follows_archived_teardown_and_frozen_acceptance(tmp_path):
    h = harness(tmp_path)
    original = h.api.delete_project

    async def delete(project_id, telegram_id):
        retained = json.loads(h.store.path.read_text())
        assert retained["verdict"]["status"] == "passed"
        assert retained["observations"]["install_after_scaffold"]["status"] == "observed"
        assert retained["teardown"]["status"] == "completed"
        assert retained["teardown"]["project_status"] == "archived"
        return await original(project_id, telegram_id)

    h.api.delete_project = delete

    outcome = await h.buyer().run()

    assert outcome.exit_code == 0
    assert PROJECT not in h.world.projects
    assert UNRELATED_PROJECT in h.world.projects
    assert h.world.cleanup_witnesses == [{"project_status": "archived", "telegram_id": BUYER}]
    assert h.api.calls.index("request_teardown") < h.api.calls.index("teardown_state")
    assert h.api.calls.index("teardown_state") < h.api.calls.index("delete_project")
    assert h.api.calls.index("delete_project") < h.api.calls.index("deletion_confirmed")
    assert h.store.record["deletion"]["delete_status"] == 204
    assert h.store.record["deletion"]["get_status"] == 404


@pytest.mark.parametrize("status", ["failed", "unknown", "pending"])
async def test_uncompleted_teardown_cannot_delete(tmp_path, status):
    h = harness(tmp_path)
    h.world.teardown = status

    async def state(project_id, telegram_id):
        h.api.calls.append("teardown_state")
        return {"status": status, "project_status": "active"}

    h.api.teardown_state = state
    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup, outcome.exit_code) == ("passed", "failed", 1)
    assert PROJECT in h.world.projects
    assert "delete_project" not in h.api.calls


@pytest.mark.parametrize("change", ["owner", "initiating_run"])
async def test_replaced_ownership_refuses_both_cleanup_stages(tmp_path, change):
    h = harness(tmp_path)

    def replace():
        if h.world.stage == "ordered":
            key, value = ("owner_id", 99) if change == "owner" else ("initiating_run_id", "other")
            h.world.projects[PROJECT][key] = value

    h.clock.hooks.append(replace)
    outcome = await h.buyer().run()

    assert outcome.cleanup == "refused"
    assert "request_teardown" not in h.api.calls
    assert "delete_project" not in h.api.calls


@pytest.mark.parametrize("change", ["refused", "still_visible", "unreadable"])
async def test_delete_requires_204_and_owner_get_404(tmp_path, change):
    h = harness(tmp_path)
    if change == "refused":
        h.world.delete_status = 409
    elif change == "still_visible":
        h.world.deleted_visible = True
    else:
        h.api.refuse.add("deletion_confirmed")

    outcome = await h.buyer().run()

    assert (outcome.verdict, outcome.cleanup, outcome.exit_code) == ("passed", "failed", 1)
    assert h.store.record["teardown"]["status"] == "completed"
    assert h.store.record["deletion"]["status"] == "failed"


async def test_teardown_refusal_is_recorded_without_deletion(tmp_path):
    h = harness(tmp_path)
    h.api.refuse.add("request_teardown")

    result = await h.buyer().run()

    assert (result.verdict, result.cleanup, result.exit_code) == ("passed", "failed", 1)
    assert h.store.record["teardown"]["http_status"] == 503
    assert "delete_project" not in h.api.calls


async def test_post_completion_still_requires_independent_teardown_readback(tmp_path):
    h = harness(tmp_path)
    h.world.teardown = "failed"

    async def request(project_id, telegram_id):
        h.api.calls.append("request_teardown")
        h.world.projects[project_id]["status"] = "archived"
        return {"status": "completed", "project_status": "archived"}

    h.api.request_teardown = request
    result = await h.buyer().run()

    assert (result.verdict, result.cleanup, result.exit_code) == ("passed", "failed", 1)
    assert "teardown_state" in h.api.calls
    assert "delete_project" not in h.api.calls
