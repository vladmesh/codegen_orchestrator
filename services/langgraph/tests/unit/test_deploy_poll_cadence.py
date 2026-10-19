"""The deployer polls deploy completion every 3 s through the real GitHub client waiters.

GitHub Actions is a `respx` fake that answers scripted run states; sleeps are recorded,
never slept, so the order of reads and waits is the evidence, not wall-clock timing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from shared.clients.github import GitHubAppClient
from src.subgraphs.devops.deployer import DeployerNode
from tests.unit.deploy_fences import held_deploy_fence

BUILT_SHA = "b" * 40
RUN_ID = 7
RUNS_PATH = "/repos/org/product/actions/workflows/deploy.yml/runs"
RUN_PATH = f"/repos/org/product/actions/runs/{RUN_ID}"


def _run(status: str, conclusion: str | None = None) -> dict:
    return {
        "id": RUN_ID,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/org/product/actions/runs/{RUN_ID}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "head_sha": BUILT_SHA,
    }


class Actions:
    """Answers each run read with the next scripted state and logs every read and sleep."""

    def __init__(self, listed: list[dict | None], by_id: list[dict]) -> None:
        self.listed = listed
        self.by_id = by_id
        self.events: list[tuple] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == RUNS_PATH:
            self.events.append(("list",))
            run = self.listed.pop(0)
            return httpx.Response(200, json={"workflow_runs": [run] if run else []})
        if request.method == "GET" and path == RUN_PATH:
            self.events.append(("read",))
            return httpx.Response(200, json=self.by_id.pop(0))
        if request.method == "POST" and path.endswith("/rerun-failed-jobs"):
            self.events.append(("rerun",))
            return httpx.Response(201)
        if request.method == "POST" and path.endswith("/dispatches"):
            self.events.append(("dispatch",))
            return httpx.Response(204)
        if path.startswith("/repos/org/product/git/refs"):
            self.events.append(("tag", request.method))
            return httpx.Response(201 if request.method == "POST" else 204, json={})
        # Failure-log reads: the waiter reports a failure without them.
        return httpx.Response(404)

    async def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))


async def _deploy(actions: Actions, monkeypatch) -> tuple[dict, bool]:
    github = GitHubAppClient()
    github.get_token = AsyncMock(return_value="github-token")
    monkeypatch.setattr(asyncio, "sleep", actions.sleep)
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.github.com").mock(side_effect=actions.handle)
        return await DeployerNode()._dispatch_and_wait(
            github, "org", "product", BUILT_SHA, None, held_deploy_fence()
        )


@pytest.mark.asyncio
async def test_dispatch_rereads_a_pending_run_every_three_seconds_and_stops_on_success(
    monkeypatch,
):
    actions = Actions(
        listed=[None, _run("in_progress"), _run("completed", "success")],
        by_id=[],
    )

    run_info, rerun = await _deploy(actions, monkeypatch)

    assert (run_info["id"], run_info["conclusion"], rerun) == (RUN_ID, "success", False)
    assert actions.events == [
        ("tag", "POST"),
        ("dispatch",),
        ("list",),
        ("sleep", 3),
        ("list",),
        ("sleep", 3),
        ("list",),
        ("tag", "DELETE"),
    ]


@pytest.mark.asyncio
async def test_an_accepted_rerun_settles_once_then_polls_its_run_every_three_seconds(
    monkeypatch,
):
    failed = _run("completed", "failure")
    actions = Actions(
        listed=[failed, failed],
        by_id=[_run("in_progress"), _run("completed", "success")],
    )

    run_info, rerun = await _deploy(actions, monkeypatch)

    assert (run_info["id"], run_info["conclusion"], rerun) == (RUN_ID, "success", True)
    assert actions.events == [
        ("tag", "POST"),
        ("dispatch",),
        ("list",),
        ("list",),
        ("rerun",),
        ("sleep", 3),
        ("read",),
        ("sleep", 3),
        ("read",),
        ("tag", "DELETE"),
    ]
