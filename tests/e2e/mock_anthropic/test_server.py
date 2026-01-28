"""Tests for Mock Anthropic Server."""

from fastapi.testclient import TestClient

from .server import app

client = TestClient(app)


class TestMockAnthropicServer:
    """Tests for the mock Anthropic API server."""

    def test_health_endpoint(self):
        """Health endpoint returns ok."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_messages_non_streaming(self):
        """Non-streaming messages endpoint returns valid response."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hello, how are you?"}],
            },
        )
        assert response.status_code == 200
        data = response.json()

        # Verify response structure
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert data["stop_reason"] == "end_turn"
        assert "id" in data
        assert data["id"].startswith("msg_mock_")
        assert len(data["content"]) == 1
        assert data["content"][0]["type"] == "text"
        assert "<result>" in data["content"][0]["text"]

    def test_messages_with_clone_keyword(self):
        """Response includes git commands when 'clone' is in prompt."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "Please clone the repository and add files"}
                ],
            },
        )
        assert response.status_code == 200
        text = response.json()["content"][0]["text"]

        assert "git clone" in text or "clone" in text.lower()
        assert "<result>" in text
        assert "success" in text

    def test_messages_streaming(self):
        """Streaming messages endpoint returns SSE events."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Test streaming"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

        # Check that we received SSE events
        content = response.text
        assert "event: message_start" in content
        assert "event: content_block_start" in content
        assert "event: content_block_delta" in content
        assert "event: message_stop" in content

    def test_messages_with_content_blocks(self):
        """Handles content as array of blocks."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Implement the feature"},
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200
        text = response.json()["content"][0]["text"]
        assert "<result>" in text
