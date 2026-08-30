"""Offline regressions for the stand-only Definition-of-Done target."""

from __future__ import annotations

from typing import Any

import pytest
import test_sprint_dod as target

pytestmark = pytest.mark.needs_no_api_credential


def test_po_events_after_ignores_other_po_input_message_types(monkeypatch):
    """Telegram and reminder input must not abort completion-notification polling."""
    entries = [
        [
            "1-0",
            [
                "type",
                "user_message",
                "text",
                "status?",
                "telegram_chat_id",
                "42",
                "request_id",
                "user-request",
            ],
        ],
        [
            "2-0",
            [
                "type",
                "reminder",
                "text",
                "check the story",
                "telegram_chat_id",
                "42",
            ],
        ],
        [
            "3-0",
            [
                "type",
                "system_event",
                "event",
                "story_completed",
                "text",
                "Deployment is ready",
                "story_id",
                "story-1",
                "project_id",
                "project-1",
            ],
        ],
    ]
    monkeypatch.setattr(target, "_redis_json", lambda *_args: entries)

    events = target._po_events_after("0-0")

    assert [(event.story_id, event.event) for event in events] == [("story-1", "story_completed")]


def test_turn_observation_budget_covers_the_engineering_cold_start():
    assert target.TURN_OBSERVATION_TIMEOUT == target.ENGINEERING_TIMEOUT


@pytest.mark.asyncio
async def test_human_review_parking_uses_the_legal_story_actions():
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    class API:
        async def post(self, path: str) -> Response:
            calls.append(path)
            return Response()

    await target._start_then_park_for_human_review(API(), "story-1")

    assert calls == ["/api/stories/story-1/start", "/api/stories/story-1/human-review"]


def test_orphan_worker_check_ignores_terminal_inventory_history():
    inventory: list[dict[str, Any]] = [
        {
            "id": "finished-worker",
            "project_id": "project-1",
            "active_turn_lease": None,
            "waiting_attempt": None,
            "story_bindings": [],
        }
    ]

    target._assert_no_orphan_workers(inventory, "project-1", "story-1")


def test_orphan_worker_check_rejects_an_unbound_live_worker():
    inventory: list[dict[str, Any]] = [
        {
            "id": "live-worker",
            "project_id": "project-1",
            "active_turn_lease": {"request_id": "turn-1"},
            "waiting_attempt": None,
            "story_bindings": [],
        }
    ]

    with pytest.raises(AssertionError, match="orphan worker"):
        target._assert_no_orphan_workers(inventory, "project-1", "story-1")
