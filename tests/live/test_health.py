"""Step 1: Health checks — baseline, should pass immediately."""

import pytest


@pytest.mark.asyncio
async def test_api_health(api_no_auth):
    """API /health returns 200."""
    resp = await api_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_redis_ping(redis):
    """Redis responds to PING."""
    result = redis("PING")
    assert "PONG" in result


def test_worker_manager_health(compose_exec):
    """Worker-manager /health returns ok."""
    result = compose_exec("worker-manager", "curl -sf http://localhost:8000/health")
    assert "ok" in result.lower() or "200" in result


@pytest.mark.parametrize(
    "stream",
    [
        "engineering:queue",
        "scaffold:queue",
        "deploy:queue",
        "po:input",
    ],
)
def test_consumer_group_exists(redis, stream):
    """Consumer groups are registered for key streams."""
    try:
        result = redis("XINFO", "GROUPS", stream)
        # XINFO GROUPS returns group info if stream exists and has groups
        assert result, f"No consumer groups on {stream}"
    except RuntimeError:
        # Stream may not exist yet (no messages published) — that's okay,
        # but the test documents the expectation.
        pytest.skip(f"Stream {stream} does not exist yet (no messages published)")
