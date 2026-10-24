"""One INSTALL-only confirmed order, shared by the deploy-to-QA producer and the QA consumer tests.

The scheduler and langgraph unit suites cannot import each other, so the brief the
handoff reads and the order QA judges are the same synthetic record from here.
Its plan installs a catalog package and leaves the repository's criteria at the
scaffold's health check (`SCAFFOLD_CRITERIA`).
"""

from datetime import UTC, datetime
import uuid

from shared.contracts.acceptance import BASELINE_ACCEPTANCE_CRITERIA
from shared.contracts.dto.product_brief import ProductBriefContent, ProductBriefRead

SCAFFOLD_CRITERIA = BASELINE_ACCEPTANCE_CRITERIA

CHANNELS_CONTENT = ProductBriefContent(
    summary="A Telegram bot that tracks channels and sends a daily digest.",
    language="ru",
    must_requirements=[
        {"id": "r1", "text": "/channels lists the tracked channels"},
        {"id": "r2", "text": "/addchannel adds a channel by its link"},
        {"id": "r3", "text": "/removechannel stops tracking a channel"},
        {"id": "r4", "text": "/digest sends the digest of tracked channels"},
        {"id": "r5", "text": "The bot answers in Russian"},
    ],
    usage_examples=[
        {"requirement_id": "r1", "user_sends": "/channels", "product_answers": "Список каналов"},
        {
            "requirement_id": "r2",
            "user_sends": "/addchannel https://t.me/example",
            "product_answers": "Канал добавлен",
        },
        {
            "requirement_id": "r3",
            "user_sends": "/removechannel https://t.me/example",
            "product_answers": "Канал удалён",
        },
        {"requirement_id": "r4", "user_sends": "/digest", "product_answers": "Дайджест за сегодня"},
    ],
)


def channels_brief(*, confirmed: bool = True) -> ProductBriefRead:
    """The brief revision as `GET /product-briefs/by-story/{story_id}` returns it."""
    return ProductBriefRead(
        id="brief-channels",
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        story_id="story-1",
        revision=2,
        title="Channel digest bot",
        content=CHANNELS_CONTENT,
        confirmed_at=datetime(2026, 1, 1, tzinfo=UTC) if confirmed else None,
        planning_attempt_active=False,
    )
