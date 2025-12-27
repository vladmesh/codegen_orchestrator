import asyncio
import json
import os

import httpx
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
INCOMING_STREAM = "telegram:incoming"
OUTGOING_STREAM = "telegram:outgoing"


async def publish_message(user_id: int, chat_id: int, text: str):
    """Publish a message to the incoming stream as if from Telegram."""
    client = aioredis.from_url(REDIS_URL, decode_responses=True)

    message_data = {
        "user_id": user_id,  # This is telegram_id
        "chat_id": chat_id,
        "message_id": 12345,
        "text": text,
        "correlation_id": f"test-{user_id}-{chat_id}",
    }

    # Publish to stream
    await client.xadd(INCOMING_STREAM, {"data": json.dumps(message_data)})
    print(f"✅ Published message to {INCOMING_STREAM}")
    print(f"   User ID (telegram_id): {user_id}")
    print(f"   Text: {text}")

    await client.aclose()


async def verify_api_access(user_id: int, other_user_id: int):
    """Verify project visibility via API."""
    print("\n🔍 Verifying API access control...")

    if API_BASE_URL.rstrip("/").endswith("/api"):
        raise RuntimeError("API_BASE_URL must not include /api")

    async with httpx.AsyncClient(base_url=f"{API_BASE_URL.rstrip('/')}/api") as client:
        # 1. Check owner access
        print(f"   Checking access for Owner (ID: {user_id})...")
        resp = await client.get("/projects/", headers={"X-Telegram-ID": str(user_id)})
        if resp.status_code == httpx.codes.OK:
            projects = resp.json()
            print(f"   ✅ Owner sees {len(projects)} projects")
            for p in projects:
                print(f"      - {p['name']} (status: {p['status']}, owner_id: {p.get('owner_id')})")
        else:
            print(f"   ❌ Failed to list projects: {resp.status_code} {resp.text}")

        # 2. Check other user access
        print(f"   Checking access for Other User (ID: {other_user_id})...")
        resp = await client.get("/projects/", headers={"X-Telegram-ID": str(other_user_id)})
        if resp.status_code == httpx.codes.OK:
            projects = resp.json()
            print(f"   ✅ Other user sees {len(projects)} projects")
            # Should be 0 if isolation works
            if len(projects) == 0:
                print("      (Correct: No projects visible)")
            else:
                print("      ⚠️ WARNING: Projects are visible to other user!")
        else:
            print(f"   ❌ Failed to list projects: {resp.status_code} {resp.text}")


async def get_last_message_id():
    """Get the ID of the last message in the outgoing stream."""
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    try:
        messages = await client.xrevrange(OUTGOING_STREAM, count=1)
        if messages:
            return messages[0][0]
    except Exception as e:
        print(f"Warning: could not get last message ID: {e}")
    finally:
        await client.aclose()
    return "0-0"


async def read_response(chat_id: int, start_id: str, timeout_seconds: int = 60):
    """Read response from outgoing stream starting after start_id."""
    client = aioredis.from_url(REDIS_URL, decode_responses=True)

    print(f"\n⏳ Waiting for response (timeout: {timeout_seconds}s, starting from {start_id})...")

    start_time = asyncio.get_event_loop().time()
    last_id = start_id

    while True:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed > timeout_seconds:
            print("⚠️ Timeout waiting for response")
            break

        messages = await client.xread({OUTGOING_STREAM: last_id}, count=10, block=1000)

        if messages:
            for _, stream_messages in messages:
                for msg_id, fields in stream_messages:
                    last_id = msg_id
                    try:
                        data = json.loads(fields.get("data", "{}"))
                        # Just looking for any response to our chat
                        if data.get("chat_id") == chat_id:
                            print("\n📨 Response received!")
                            response_text = data.get("text", "")
                            print(f"   Text: {response_text[:200]}...")

                            # Wait a bit for async API calls to finish inside worker
                            print("   Wait 2s for consistency...")
                            await asyncio.sleep(2)
                            await client.aclose()
                            return True
                    except json.JSONDecodeError:
                        pass

    await client.aclose()
    return False


async def main():
    # Test user: vladmesh (telegram_id=625038902)
    # Other user: 999111
    telegram_id = 625038902
    other_id = 999111

    # Send a message to create a project
    test_message = "Создай проект auth_test_2 - секретный проект"

    # Get stream position BEFORE sending
    last_id = await get_last_message_id()
    print(f"📍 Stream position: {last_id}")

    await publish_message(telegram_id, telegram_id, test_message)

    received = await read_response(telegram_id, last_id)

    if received:
        await verify_api_access(telegram_id, other_id)
    else:
        print("❌ Cannot verify API: No response received")


if __name__ == "__main__":
    asyncio.run(main())
