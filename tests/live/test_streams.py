"""Step 3: Redis Streams infrastructure — baseline, should pass immediately."""

import pytest

from shared.queues import DEPLOY_QUEUE, ENGINEERING_QUEUE, PO_INPUT_QUEUE, SCAFFOLD_QUEUE


def test_publish_and_read_test_stream(redis):
    """Can publish to a test stream and read it back."""
    stream = "live-test:scratch"
    # publish
    msg_id = redis("XADD", stream, "*", "key", "hello", "val", "world")
    assert msg_id, "XADD returned empty"

    # read
    result = redis("XRANGE", stream, "-", "+", "COUNT", "1")
    assert "hello" in result

    # cleanup
    redis("DEL", stream)


@pytest.mark.parametrize(
    "stream",
    [
        SCAFFOLD_QUEUE,
        ENGINEERING_QUEUE,
        DEPLOY_QUEUE,
        PO_INPUT_QUEUE,
    ],
)
def test_stream_has_consumer_group(redis, stream):
    """Key streams have at least one consumer group registered."""
    try:
        info = redis("XINFO", "GROUPS", stream)
    except RuntimeError:
        pytest.skip(f"Stream {stream} not created yet")
        return

    # XINFO GROUPS output contains "name" for each group
    assert "name" in info, f"No consumer groups on {stream}"
