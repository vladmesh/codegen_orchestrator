import os
import time

import pytest

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerConfig,
)
from shared.redis.client import RedisStreamClient


@pytest.fixture
def run_e2e_real(request):
    return request.config.getoption("--run-e2e-real")


async def wait_for_stream_message(redis: RedisStreamClient, stream: str, timeout: int = 60) -> dict:
    """Wait for a message on Redis stream."""
    start = time.time()
    while time.time() - start < timeout:
        messages = await redis.xread({stream: "0"}, count=1, block=1000)
        if messages:
            # expected structure: [[stream_name, [[message_id, {data: ...}]]]]
            return messages[0][1][0][1]
    raise TimeoutError(f"No message received on {stream} within {timeout}s")


async def cleanup_worker(redis: RedisStreamClient, worker_id: str):
    """Send delete command for worker."""
    await redis.xadd(
        "worker:commands",
        {
            "data": DeleteWorkerCommand(
                request_id=f"cleanup-{worker_id}", worker_id=worker_id
            ).model_dump_json()
        },
    )


@pytest.mark.e2e_real
@pytest.mark.skipif(
    not os.getenv("CLAUDE_SESSION_DIR") and not os.getenv("CI"),
    reason="Requires real Claude session (CLAUDE_SESSION_DIR)",
)
async def test_claude_real_session_deterministic_answer(redis: RedisStreamClient, docker_client):
    """
    Claude должен правильно ответить на математический вопрос.

    Flow:
    1. CreateWorkerCommand(agent_type=CLAUDE, auth_mode=host_session)
    2. Отправить в worker:{id}:input:
       "Ответь сколько будет шесть плюс три одним словом на русском языке"
    3. Читать из worker:{id}:output
    4. Assert "девять" in result.lower()
    """
    # 1. Create Worker
    cmd = CreateWorkerCommand(
        request_id="claude-real-test-1",
        config=WorkerConfig(
            name="claude-real-worker",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="You are a helpful assistant.",
            auth_mode="host_session",
            host_claude_dir=os.getenv("CLAUDE_SESSION_DIR", "/home/vlad/.claude"),
        ),
    )
    # Using raw xadd because CreateWorkerCommand might need to be wrapped or standardized
    # The integration tests use redis.xadd with "data" field
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})

    # Wait for creation response
    resp = await wait_for_stream_message(redis, "worker:responses:developer")
    # Parse just to get ID - assuming response is valid JSON
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")
    assert worker_id, "Failed to get worker_id"

    try:
        # 2. Send Input
        # Note: The actual messaging format might differ based on wrapper implementation.
        # Assuming worker consumes from worker:{id}:input and expects some format?
        # Actually, Claude runner (headless) typically takes task instructions on start or
        # via an input stream?
        # Checking implementation details:
        # The Orchestrator usually adds tasks to a queue or the worker listens on a channel.
        # But for 'test_worker_executes_task_with_mocked_claude', we just created the
        # worker with instructions.
        # Wait, strictly speaking, `CreateWorkerCommand` *starts* the worker.
        # If the worker is "task-based" (one-shot), the instructions ARE the task.
        # If it's a persistent session, we might need to send more inputs.
        # P1_BLOCKING_TESTS says: "Отправить в worker:{id}:input".
        # Let's assume there is an input stream for interactive communication or we
        # rely on initial instructions?
        # Re-reading P1_BLOCKING_TESTS: "2. Отправить в worker:{id}:input..."
        # This implies an interactive mode or at least a way to pipe input.
        # Let's try writing to that stream.

        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Ответь сколько будет шесть плюс три одним словом на русском языке"},
        )

        # 3. Read Output
        # output stream: worker:{worker_id}:output? Or worker:developer:output?
        # P1_BLOCKING_TESTS: "Читать из worker:{id}:output"
        output_msg = await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=120)

        # 4. Assert
        # The output format is likely AgentOutput or similar
        result_str = output_msg.get("data", "") or output_msg.get("content", "")
        # Try to parse if it's JSON
        try:
            res_json = json.loads(result_str)
            content = res_json.get("content", str(res_json))
        except Exception:
            content = str(result_str)

        assert "девять" in content.lower()

    finally:
        await cleanup_worker(redis, worker_id)


@pytest.mark.e2e_real
@pytest.mark.skipif(
    not os.getenv("CLAUDE_SESSION_DIR") and not os.getenv("CI"),
    reason="Requires real Claude session",
)
async def test_claude_real_session_memory(redis: RedisStreamClient, docker_client):
    """
    Claude должен помнить предыдущий вопрос в рамках сессии.
    """
    # Reuse flow or create new worker
    cmd = CreateWorkerCommand(
        request_id="claude-mem-test",
        config=WorkerConfig(
            name="claude-mem-worker",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            auth_mode="host_session",
            host_claude_dir=os.getenv("CLAUDE_SESSION_DIR", "/home/vlad/.claude"),
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    resp = await wait_for_stream_message(redis, "worker:responses:developer")
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")

    try:
        # Question 1
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Ответь сколько будет шесть плюс три одним словом"},
        )
        await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=60)

        # Question 2
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Верни предыдущий вопрос который я тебе задавал и только его"},
        )
        msg2 = await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=60)

        content = str(msg2)
        assert "шесть" in content.lower() and "три" in content.lower()

    finally:
        await cleanup_worker(redis, worker_id)


@pytest.mark.e2e_real
@pytest.mark.skipif(
    not os.getenv("FACTORY_API_KEY"),
    reason="Requires real FACTORY_API_KEY",
)
async def test_factory_api_key_deterministic_answer(redis: RedisStreamClient, docker_client):
    """
    Factory должен правильно ответить на математический вопрос.
    """
    cmd = CreateWorkerCommand(
        request_id="factory-real-test",
        config=WorkerConfig(
            name="factory-real-worker",
            worker_type="developer",
            agent_type=AgentType.FACTORY,
            env_vars={"FACTORY_API_KEY": os.getenv("FACTORY_API_KEY")},
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    resp = await wait_for_stream_message(redis, "worker:responses:developer")
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")

    try:
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Ответь сколько будет шесть плюс три одним словом на русском языке"},
        )
        msg = await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=60)
        content = str(msg)
        assert "девять" in content.lower()
    finally:
        await cleanup_worker(redis, worker_id)


@pytest.mark.e2e_real
@pytest.mark.skipif(
    not os.getenv("FACTORY_API_KEY"),
    reason="Requires real FACTORY_API_KEY",
)
async def test_factory_api_key_session_memory(redis: RedisStreamClient, docker_client):
    """
    Factory должен помнить предыдущий вопрос.
    """
    cmd = CreateWorkerCommand(
        request_id="factory-mem-test",
        config=WorkerConfig(
            name="factory-mem-worker",
            worker_type="developer",
            agent_type=AgentType.FACTORY,
            env_vars={"FACTORY_API_KEY": os.getenv("FACTORY_API_KEY")},
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    resp = await wait_for_stream_message(redis, "worker:responses:developer")
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")

    try:
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Ответь сколько будет шесть плюс три одним словом"},
        )
        await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=60)

        await redis.xadd(
            f"worker:{worker_id}:input",
            {"content": "Верни предыдущий вопрос который я тебе задавал и только его"},
        )
        msg = await wait_for_stream_message(redis, f"worker:{worker_id}:output", timeout=60)
        content = str(msg)
        assert "шесть" in content.lower() and "три" in content.lower()
    finally:
        await cleanup_worker(redis, worker_id)
