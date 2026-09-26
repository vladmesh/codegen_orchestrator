"""PO consumer unit-test fixtures shared across the package."""

from fakeredis.aioredis import FakeRedis
import pytest

from shared.contracts.dto.story import StoryDTO
from shared.redis import RedisStreamClient
from src.consumers import po as po_consumer
from src.consumers.po_story_gate import ProactiveStoryGate


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
