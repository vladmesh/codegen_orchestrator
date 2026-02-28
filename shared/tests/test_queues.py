"""Unit tests for shared.queues — declarative queue registry."""

from dataclasses import FrozenInstanceError

from fakeredis import aioredis
import pytest

from shared.queues import (
    DEPLOY_QUEUE,
    ENGINEERING_QUEUE,
    INFRA_GROUP,
    PO_CONSUMER_GROUP,
    PO_INPUT_QUEUE,
    PO_PROACTIVE_GROUP,
    PO_PROACTIVE_QUEUE,
    PROVISIONER_QUEUE,
    PROVISIONER_RESULTS,
    QUEUE_TOPOLOGY,
    SCHEDULER_CONSUMER_GROUP,
    TELEGRAM_BOT_GROUP,
    WORKER_COMMANDS,
    WORKER_GROUP,
    WORKER_MANAGER_GROUP,
    QueueBinding,
    ensure_all_groups,
    ensure_consumer_groups,
)


class TestQueueBinding:
    def test_frozen(self):
        b = QueueBinding(stream="s", group="g", description="d")
        with pytest.raises(FrozenInstanceError):
            b.stream = "x"

    def test_fields(self):
        b = QueueBinding(stream="s", group="g", description="d")
        assert b.stream == "s"
        assert b.group == "g"
        assert b.description == "d"


class TestQueueTopology:
    def test_has_expected_binding_count(self):
        expected_count = 8  # noqa: PLR2004
        assert len(QUEUE_TOPOLOGY) == expected_count

    def test_all_streams_present(self):
        streams = {b.stream for b in QUEUE_TOPOLOGY}
        assert ENGINEERING_QUEUE in streams
        assert DEPLOY_QUEUE in streams
        assert PROVISIONER_QUEUE in streams
        assert PROVISIONER_RESULTS in streams
        assert WORKER_COMMANDS in streams
        assert PO_INPUT_QUEUE in streams
        assert PO_PROACTIVE_QUEUE in streams

    def test_all_groups_present(self):
        groups = {b.group for b in QUEUE_TOPOLOGY}
        assert WORKER_GROUP in groups
        assert INFRA_GROUP in groups
        assert SCHEDULER_CONSUMER_GROUP in groups
        assert TELEGRAM_BOT_GROUP in groups
        assert WORKER_MANAGER_GROUP in groups
        assert PO_CONSUMER_GROUP in groups
        assert PO_PROACTIVE_GROUP in groups

    def test_provisioner_results_has_two_consumers(self):
        pr_bindings = [b for b in QUEUE_TOPOLOGY if b.stream == PROVISIONER_RESULTS]
        expected = 2  # noqa: PLR2004
        assert len(pr_bindings) == expected
        groups = {b.group for b in pr_bindings}
        assert groups == {SCHEDULER_CONSUMER_GROUP, TELEGRAM_BOT_GROUP}


class TestEnsureAllGroups:
    @pytest.fixture
    async def fake_redis(self):
        r = aioredis.FakeRedis(decode_responses=True)
        yield r
        await r.aclose()

    @pytest.mark.asyncio
    async def test_creates_all_groups(self, fake_redis):
        await ensure_all_groups(fake_redis)

        # Verify each binding was created
        for binding in QUEUE_TOPOLOGY:
            groups = await fake_redis.xinfo_groups(binding.stream)
            group_names = [g["name"] for g in groups]
            assert (
                binding.group in group_names
            ), f"Group {binding.group} missing on {binding.stream}"

    @pytest.mark.asyncio
    async def test_idempotent(self, fake_redis):
        """Calling twice should not raise."""
        await ensure_all_groups(fake_redis)
        await ensure_all_groups(fake_redis)

    @pytest.mark.asyncio
    async def test_backward_compat_alias(self, fake_redis):
        """ensure_consumer_groups is an alias for ensure_all_groups."""
        assert ensure_consumer_groups is ensure_all_groups
        # Also works when called
        await ensure_consumer_groups(fake_redis)
