"""PO consumer unit-test fixtures shared across the package."""

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.dto.product_brief import ProductBriefRead
from shared.contracts.dto.story import StoryDTO
from shared.redis import RedisStreamClient
from src.consumers import po as po_consumer
from src.consumers.po_story_gate import ProactiveStoryGate
from tests.unit.factories import make_product_brief


@pytest.fixture(autouse=True)
def _fixed_po_summarization_config(monkeypatch):
    """Keep consumer-behaviour tests independent of the system-config API.

    These tests exercise Redis delivery/PEL semantics, not production config
    loading. Production startup coverage lives in test_agent_llm_env.py.
    """
    monkeypatch.setattr(
        po_consumer,
        "load_summarization_config",
        lambda _api_base_url: po_consumer.SummarizationConfig(
            max_tokens=1,
            trigger_tokens=1,
            max_summary_tokens=1,
        ),
    )
    monkeypatch.setattr(po_consumer, "load_story_gate_cap", lambda _api_base_url: lambda: 6)


class InWorkStories:
    """The API's stories as the PO gate reads them: in work unless a test moves one."""

    def __init__(self) -> None:
        self.overrides: dict[str, dict] = {}

    def put(self, story_id: str, **fields) -> None:
        self.overrides[story_id] = {**self.overrides.get(story_id, {}), **fields}

    async def get_story(self, story_id: str) -> StoryDTO:
        return StoryDTO.model_validate(
            {
                "id": story_id,
                "project_id": "00000000-0000-0000-0000-000000000001",
                "title": "Story",
                "type": "product",
                "status": "in_progress",
                "waiting_on": "none",
                "priority": 0,
                "created_by": "po",
                "created_at": "2026-09-26T10:00:00+00:00",
                "updated_at": "2026-09-26T10:00:00+00:00",
                **self.overrides.get(story_id, {}),
            }
        )


@pytest.fixture
def gate_stories() -> InWorkStories:
    return InWorkStories()


@pytest.fixture
def gate_redis() -> RedisStreamClient:
    client = RedisStreamClient(redis_url="redis://localhost:6379/0")
    client._redis = FakeRedis(decode_responses=True)
    return client


@pytest.fixture(autouse=True)
def story_gate(monkeypatch, gate_redis, gate_stories) -> ProactiveStoryGate:
    """Every consumer test runs with the real gate over fakeredis and in-work stories."""
    gate = ProactiveStoryGate(gate_redis, gate_stories, lambda: 6)
    monkeypatch.setattr(po_consumer, "_story_gate", gate)
    return gate


class OrderedStories:
    """The API's brief-by-story read: every story is ordered unless a test says otherwise.

    ``unordered`` stories answer as the API's 404; ``failing`` ones raise the
    error a read that cannot be answered raises.
    """

    def __init__(self) -> None:
        self.unordered: set[str] = set()
        self.unconfirmed: set[str] = set()
        self.failing: dict[str, Exception] = {}
        self.reads: list[str] = []

    async def get_product_brief_by_story(self, story_id: str) -> ProductBriefRead | None:
        self.reads.append(story_id)
        if story_id in self.failing:
            raise self.failing[story_id]
        if story_id in self.unordered:
            return None
        if story_id in self.unconfirmed:
            return make_product_brief(
                story_id=story_id, confirmed_at=None, confirmation_request_id=None
            )
        return make_product_brief(story_id=story_id)


@pytest.fixture(autouse=True)
def ordered_stories(monkeypatch) -> OrderedStories:
    """Every consumer test runs over stories that are ordered unless it moves one."""
    stories = OrderedStories()
    monkeypatch.setattr(
        po_consumer.api_client, "get_product_brief_by_story", stories.get_product_brief_by_story
    )
    return stories
