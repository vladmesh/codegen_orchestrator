"""Mock Anthropic API Server for E2E Testing.

This server mimics the Anthropic Messages API to allow testing
Developer workers without consuming real API credits.

Supports:
- POST /v1/messages (non-streaming)
- POST /v1/messages with stream=true (SSE streaming)
"""

import json
import time
from typing import Any
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from .responses import get_response_for_prompt

app = FastAPI(title="Mock Anthropic API", version="1.0.0")


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/v1/messages")
async def create_message(request: Request):
    """Mock implementation of Anthropic Messages API.

    Handles both streaming and non-streaming requests.
    Parses the prompt to determine which mock response to return.
    """
    body = await request.json()

    model = body.get("model", "claude-sonnet-4-20250514")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # Extract the last user message for response selection
    # Concatenate all text blocks to get full context including the actual prompt
    last_user_message = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                last_user_message = content
            elif isinstance(content, list):
                # Handle content blocks - concatenate ALL text blocks
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                last_user_message = "\n".join(text_parts)
            break

    # Get mock response based on prompt content
    response_text = get_response_for_prompt(last_user_message)

    if stream:
        return StreamingResponse(
            _stream_response(response_text, model),
            media_type="text/event-stream",
        )
    else:
        return _create_message_response(response_text, model)


def _create_message_response(text: str, model: str) -> dict[str, Any]:
    """Create a non-streaming message response."""
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": len(text) // 4},
    }


async def _stream_response(text: str, model: str):
    """Generate SSE stream for streaming response.

    Anthropic streaming format:
    - message_start: Initial message metadata
    - content_block_start: Start of content block
    - content_block_delta: Text chunks
    - content_block_stop: End of content block
    - message_delta: Final message metadata
    - message_stop: End of message
    """
    message_id = f"msg_mock_{uuid.uuid4().hex[:12]}"

    # message_start event
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 0},
            },
        },
    )

    # content_block_start event
    yield _sse_event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )

    # Stream text in chunks
    chunk_size = 20
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
        yield _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": chunk},
            },
        )
        # Small delay to simulate real streaming
        time.sleep(0.01)

    # content_block_stop event
    yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})

    # message_delta event
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": len(text) // 4},
        },
    )

    # message_stop event
    yield _sse_event("message_stop", {"type": "message_stop"})


def _sse_event(event_type: str, data: dict) -> str:
    """Format data as Server-Sent Event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104
