#!/usr/bin/env python3
"""Test script for agent message bridge.

Tests the full message flow:
1. Telegram Bot -> agent:incoming (PubSub)
2. agent-spawner -> Claude container
3. agent-spawner -> agent:outgoing (Stream)
4. Telegram Bot reads from agent:outgoing
"""

import asyncio
import json
import time

import redis.asyncio as redis


async def test_message_flow():
    """Test full message flow through agent bridge."""
    print("Connecting to Redis...")
    r = await redis.from_url("redis://localhost:6379")

    try:
        # Step 1: Publish test message to agent:incoming
        test_message = {
            "user_id": "123456",
            "message": "Hello, what can you do?",
            "chat_id": 123456,
            "message_id": 1,
            "correlation_id": f"test_{int(time.time())}",
        }

        print("\n1. Publishing test message to agent:incoming...")
        print(f"   Message: {test_message}")

        await r.publish("agent:incoming", json.dumps(test_message))
        print("   ✓ Published")

        # Step 2: Wait a bit for agent-spawner to process
        print("\n2. Waiting for agent-spawner to process...")
        await asyncio.sleep(2)

        # Step 3: Check agent-spawner logs for processing
        print("\n3. Checking if message was received by agent-spawner...")
        print("   (Check docker logs: docker compose logs agent-spawner --tail 20)")

        # Step 4: Try to read from agent:outgoing stream
        print("\n4. Checking agent:outgoing stream for response...")

        # Read last messages from stream
        messages = await r.xrevrange("agent:outgoing", count=5)

        if not messages:
            print("   ⚠ No messages in agent:outgoing stream")
            print("   This is expected if ANTHROPIC_API_KEY is not set")
            print("   or if agent-spawner hasn't processed the message yet")
        else:
            print(f"   ✓ Found {len(messages)} messages in stream")
            for msg_id, data in messages:
                print(f"\n   Message ID: {msg_id.decode()}")
                for key, value in data.items():
                    print(f"     {key.decode()}: {value.decode()}")

        # Step 5: Check consumer group
        print("\n5. Checking consumer group status...")
        try:
            groups = await r.xinfo_groups("agent:outgoing")
            print(f"   ✓ Found {len(groups)} consumer groups:")
            for group in groups:
                print(f"     - {group['name'].decode()}: {group['consumers']} consumers")
        except redis.ResponseError as e:
            print(f"   ⚠ {e}")

        print("\n" + "=" * 60)
        print("Test completed!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Set ANTHROPIC_API_KEY in .env to test full flow")
        print("2. Or check logs: docker compose logs agent-spawner -f")
        print("3. To test with real message, send text to Telegram bot")

    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(test_message_flow())
