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

pytestmark = pytest.mark.asyncio


@pytest.fixture
def run_e2e_real(request):
    return request.config.getoption("--run-e2e-real")


async def wait_for_stream_message(
    redis: RedisStreamClient, stream: str, timeout: int = 300, last_id: str = "0"
) -> tuple[str, dict]:
    """Wait for a message on Redis stream. Returns (message_id, data)."""
    start = time.time()
    while time.time() - start < timeout:
        # Use > if last_id is provided and we want new messages, but xread takes explicit ID.
        # If last_id is "0", it gets all. If we want only new relative to last_id, we pass last_id.
        # But for xread with specific ID, it returns generic stream.
        # Actually xread({stream: last_id}) returns messages with ID > last_id.
        messages = await redis.xread({stream: last_id}, count=1, block=1000)
        if messages:
            # expected structure: [[stream_name, [[message_id, {data: ...}]]]]
            msg_id = messages[0][1][0][0]
            data = messages[0][1][0][1]
            return msg_id, data
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
    # Clear streams to ensure fresh start
    await redis.delete("worker:responses:developer")

    # 1. Create Worker
    request_id = f"claude-real-{int(time.time())}"
    cmd = CreateWorkerCommand(
        request_id=request_id,
        config=WorkerConfig(
            name="claude-real-worker",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="You are a helpful assistant.",
            auth_mode="host_session",
            host_claude_dir="/host-claude",
            allowed_commands=["*"],
            capabilities=[],
        ),
    )
    # Using raw xadd because CreateWorkerCommand might need to be wrapped or standardized
    # The integration tests use redis.xadd with "data" field
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})

    # Wait for creation response
    _, resp = await wait_for_stream_message(redis, "worker:responses:developer")
    # Parse just to get ID - assuming response is valid JSON
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")
    assert worker_id, "Failed to get worker_id"

    try:
        # 2. Send Input

        await redis.xadd(
            f"worker:{worker_id}:input",
            {
                "data": json.dumps(
                    {"content": "Ответь сколько будет шесть плюс три одним словом на русском языке"}
                )
            },
        )

        # 3. Read Output
        # output stream: worker:{worker_id}:output? Or worker:developer:output?
        # P1_BLOCKING_TESTS: "Читать из worker:{id}:output"
        _, output_msg = await wait_for_stream_message(
            redis, f"worker:{worker_id}:output", timeout=120
        )

        # 4. Assert
        # The output format is likely AgentOutput or similar
        result_str = output_msg.get("data", "") or output_msg.get("content", "")
        # Try to parse if it's JSON
        try:
            res_json = json.loads(result_str)
            if res_json.get("status") == "no_structured_result" and "raw_output" in res_json:
                # Handle raw output from Claude CLI
                raw = res_json["raw_output"]
                try:
                    inner = json.loads(raw)
                    content = inner.get("result", str(inner))
                except json.JSONDecodeError:
                    content = raw
            else:
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

    Flow:
    1. Create worker
    2. Send Question 1: "Ответь сколько будет шесть плюс три одним словом"
    3. Wait for answer
    4. Send Question 2: "Верни предыдущий вопрос который я тебе задавал"
    5. Assert that response contains "шесть" and "три" (proving memory)
    """
    import json

    # Clear streams
    await redis.delete("worker:responses:developer")

    cmd = CreateWorkerCommand(
        request_id=f"claude-mem-{int(time.time())}",
        config=WorkerConfig(
            name="claude-mem-worker",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="You are a helpful assistant.",
            auth_mode="host_session",
            host_claude_dir="/host-claude",
            allowed_commands=["*"],
            capabilities=[],
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    _, resp = await wait_for_stream_message(redis, "worker:responses:developer")

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")
    assert worker_id, "Failed to get worker_id"

    last_output_id = "0"

    try:
        # Question 1
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"data": json.dumps({"content": "Ответь сколько будет шесть плюс три одним словом"})},
        )
        last_output_id, q1_msg = await wait_for_stream_message(
            redis, f"worker:{worker_id}:output", timeout=120, last_id=last_output_id
        )
        print(f"[DEBUG] Q1 response received, msg_id={last_output_id}")

        # Question 2 - test session memory
        await redis.xadd(
            f"worker:{worker_id}:input",
            {
                "data": json.dumps(
                    {"content": "Верни предыдущий вопрос который я тебе задавал и только его"}
                )
            },
        )
        _, q2_msg = await wait_for_stream_message(
            redis, f"worker:{worker_id}:output", timeout=120, last_id=last_output_id
        )

        # Parse Q2 response
        result_str = q2_msg.get("data", "") or q2_msg.get("content", "")
        try:
            res_json = json.loads(result_str)
            if res_json.get("status") == "no_structured_result" and "raw_output" in res_json:
                raw = res_json["raw_output"]
                try:
                    inner = json.loads(raw)
                    content = inner.get("result", str(inner))
                except json.JSONDecodeError:
                    content = raw
            else:
                content = res_json.get("content", str(res_json))
        except Exception:
            content = str(result_str)

        print(f"[DEBUG] Q2 content: {content}")

        # Assert memory works - Claude should remember the previous question
        assert (
            "шесть" in content.lower() and "три" in content.lower()
        ), f"Session memory failed. Expected previous question about 6+3. Got: {content}"

    except Exception:
        # Dump logs for debugging
        try:
            c = docker_client.containers.get(f"worker-{worker_id}")
            print(f"\n[DEBUG] Logs for worker-{worker_id}:\n{c.logs().decode()}\n[DEBUG] End Logs")
        except Exception as log_err:
            print(f"\n[DEBUG] Failed to get logs: {log_err}")
        raise

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
    # Clear streams to ensure fresh start
    await redis.delete("worker:responses:developer")

    request_id = f"factory-real-{int(time.time())}"
    cmd = CreateWorkerCommand(
        request_id=request_id,
        config=WorkerConfig(
            name="factory-real-worker",
            worker_type="developer",
            agent_type=AgentType.FACTORY,
            env_vars={"FACTORY_API_KEY": os.getenv("FACTORY_API_KEY")},
            instructions="You are a helpful assistant.",
            capabilities=["git", "curl"],
            allowed_commands=["git", "curl", "droid"],
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    _, resp = await wait_for_stream_message(redis, "worker:responses:developer")
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")

    try:
        await redis.xadd(
            f"worker:{worker_id}:input",
            {
                "data": json.dumps(
                    {"content": "Ответь сколько будет шесть плюс три одним словом на русском языке"}
                )
            },
        )
        _, msg = await wait_for_stream_message(redis, f"worker:{worker_id}:output")

        # 4. Assert
        # The output format is likely AgentOutput or similar
        result_str = msg.get("data", "") or msg.get("content", "")
        # Try to parse if it's JSON
        try:
            res_json = json.loads(result_str)

            # Case: Agent returned a JSON string inside 'raw_output' if parsing failed
            # (e.g. due to CLI noise)
            if res_json.get("status") == "no_structured_result" and "raw_output" in res_json:
                raw = res_json["raw_output"]
                # Clean up known noise (e.g. bell char)
                raw = raw.replace("\u0007", "")
                try:
                    inner_json = json.loads(raw)
                    # Factory/Claude agent result field
                    content = inner_json.get("result", str(inner_json))
                except json.JSONDecodeError:
                    content = raw
            else:
                content = res_json.get("content", str(res_json))
        except Exception:
            content = str(result_str)

        print(f"DEBUG: Parsed content: {content}")
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
            instructions="You are a helpful assistant.",
            allowed_commands=["*"],
            capabilities=[],
        ),
    )
    await redis.xadd("worker:commands", {"data": cmd.model_dump_json()})
    _, resp = await wait_for_stream_message(redis, "worker:responses:developer")
    import json

    data = json.loads(resp["data"])
    worker_id = data.get("worker_id")

    last_output_id = "0"

    try:
        await redis.xadd(
            f"worker:{worker_id}:input",
            {"data": json.dumps({"content": "Ответь сколько будет шесть плюс три одним словом"})},
        )
        last_output_id, _ = await wait_for_stream_message(
            redis, f"worker:{worker_id}:output", last_id=last_output_id
        )

        await redis.xadd(
            f"worker:{worker_id}:input",
            {
                "data": json.dumps(
                    {"content": "Верни предыдущий вопрос который я тебе задавал и только его"}
                )
            },
        )
        _, msg = await wait_for_stream_message(
            redis, f"worker:{worker_id}:output", last_id=last_output_id
        )
        content = str(msg)
        assert "шесть" in content.lower() and "три" in content.lower()
    finally:
        await cleanup_worker(redis, worker_id)
