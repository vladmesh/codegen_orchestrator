import asyncio
import json
import os
import time
import uuid

import redis.asyncio as redis

from shared.redis_client import RedisStreamClient

# Configure Redis URL
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
TIMEOUT = 30


async def run_test():  # noqa: C901
    print(f"🔌 Connecting to Redis at {REDIS_URL}...")
    client = redis.from_url(REDIS_URL, decode_responses=True)

    # Test Case 1: Delegation
    print("\n🧪 TEST CASE 1: Delegation (Should go to Analyst)")
    chat_id = 12345
    user_id = 999

    str(uuid.uuid4())
    payload = {
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": 101,
        "text": "Создай новый проект для прогноза погоды",
        "thread_id": f"user_{user_id}",
        "correlation_id": f"test_delegation_{int(time.time())}",
    }

    print(f"📤 Sending: {payload['text']}")

    # Get last ID from outgoing stream before we send, to ensure we catch the response
    try:
        last_entries = await client.xrevrange(RedisStreamClient.OUTGOING_STREAM, count=1)
        last_id = last_entries[0][0] if last_entries else "0-0"
    except Exception:
        last_id = "0-0"

    await client.xadd(RedisStreamClient.INCOMING_STREAM, {"data": json.dumps(payload)})

    # Wait for response
    print("⏳ Waiting for response...")
    start_time = time.time()
    delegation_confirmed = False

    while time.time() - start_time < TIMEOUT:
        # Read new messages since last_id
        streams = await client.xread(
            {RedisStreamClient.OUTGOING_STREAM: last_id}, count=1, block=1000
        )

        if not streams:
            continue

        for _stream_name, messages in streams:
            for message_id, fields in messages:
                last_id = message_id  # Update last_id for next iteration
                data = json.loads(fields["data"])
                if data.get("chat_id") == chat_id:
                    response_text = data.get("text", "")
                    print(f"dml📥 Received: {response_text}")

                    if "Аналитику" in response_text or "Analyst" in response_text:
                        print("✅ SUCCESS: Delegation response received!")
                        delegation_confirmed = True
                        break

        if delegation_confirmed:
            break

    if not delegation_confirmed:
        print("❌ FAILED: Timeout waiting for delegation response")

    # Test Case 2: Non-Delegation
    print("\n🧪 TEST CASE 2: Non-Delegation (Should stay with PO)")
    str(uuid.uuid4())
    payload = {
        "user_id": user_id,
        "chat_id": chat_id,
        "message_id": 102,
        "text": "Какие есть проекты?",
        "thread_id": f"user_{user_id}",
        "correlation_id": f"test_status_{int(time.time())}",
    }

    print(f"📤 Sending: {payload['text']}")

    # Get last ID from outgoing stream before we send
    try:
        last_entries = await client.xrevrange(RedisStreamClient.OUTGOING_STREAM, count=1)
        last_id = last_entries[0][0] if last_entries else "0-0"
    except Exception:
        last_id = "0-0"

    await client.xadd(RedisStreamClient.INCOMING_STREAM, {"data": json.dumps(payload)})

    # Wait for response
    print("⏳ Waiting for response...")
    start_time = time.time()
    direct_response_confirmed = False

    while time.time() - start_time < TIMEOUT:
        streams = await client.xread(
            {RedisStreamClient.OUTGOING_STREAM: last_id}, count=1, block=1000
        )

        if not streams:
            continue

        for _stream_name, messages in streams:
            for message_id, fields in messages:
                last_id = message_id
                data = json.loads(fields["data"])
                if data.get("chat_id") == chat_id:
                    response_text = data.get("text", "")
                    print(f"📥 Received: {response_text}")

                    # Should NOT have "Analyst" in standard PO response for listing projects
                    if "Проектов пока нет" in response_text or "Проекты:" in response_text:
                        print("✅ SUCCESS: PO Direct response received!")
                        direct_response_confirmed = True
                        break

        if direct_response_confirmed:
            break

    if not direct_response_confirmed:
        print("❌ FAILED: Timeout waiting for PO response")

    await client.aclose()


if __name__ == "__main__":
    asyncio.run(run_test())
