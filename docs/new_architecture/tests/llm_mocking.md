# LLM Mocking Strategy

Стратегия мокирования LLM для тестов.

## 1. Два типа LLM-взаимодействий

```
┌─────────────────────────────────────────────────────────────┐
│  Type 1: CLI Agent (subprocess)                             │
│                                                             │
│  Worker Wrapper → subprocess → claude --headless --prompt   │
│                                                             │
│  Используется: Developer Worker, PO Worker                  │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Type 2: Direct LLM API (LangGraph nodes)                   │
│                                                             │
│  Node → langchain/openai SDK → api.openai.com               │
│                                                             │
│  Используется: Analyst, другие LLM-ноды (если будут)        │
└─────────────────────────────────────────────────────────────┘
```

## 2. CLI Agent Mocking

### 2.1 Key Discovery: Custom Endpoint

Claude Code поддерживает переопределение API endpoint:

```bash
export ANTHROPIC_BASE_URL="http://mock-server:8000"
export ANTHROPIC_AUTH_TOKEN="test-key"

claude -p "Create hello.py"
# → Запрос пойдёт на mock-server вместо api.anthropic.com
```

**Sources:**
- [Claude Code Headless Mode](https://code.claude.com/docs/en/headless)
- [Custom Endpoint Support](https://github.com/anthropics/claude-code/issues/216)
- [Claude Code + LiteLLM](https://docs.litellm.ai/docs/tutorials/claude_responses_api)

### 2.2 Test Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Integration Test Setup                                      │
│                                                             │
│  Worker Container                     Mock Anthropic Server │
│  ┌─────────────────┐                 ┌─────────────────┐   │
│  │ worker-wrapper  │                 │ FastAPI app     │   │
│  │                 │                 │                 │   │
│  │ Spawns:         │                 │ /v1/messages    │   │
│  │ claude -p "..." │ ───────────────►│ Returns scripted│   │
│  │                 │ ◄───────────────│ responses       │   │
│  │ ANTHROPIC_BASE  │                 │                 │   │
│  │ =mock:8000      │                 │ Tool calls work │   │
│  └─────────────────┘                 └─────────────────┘   │
│         │                                                   │
│         ▼                                                   │
│  ┌─────────────────┐                                       │
│  │ /tmp/repo       │  ← Real files created by Claude CLI   │
│  │ - hello.py      │                                       │
│  │ - ...           │                                       │
│  └─────────────────┘                                       │
└─────────────────────────────────────────────────────────────┘
```

### 2.3 Mock Anthropic Server

```python
# tests/fixtures/mock_anthropic_server.py

from fastapi import FastAPI, Request
from pydantic import BaseModel
import re

app = FastAPI()

# Scripted responses: prompt pattern → response
RESPONSES: dict[str, dict] = {
    "create hello.py": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I'll create hello.py for you."},
            {
                "type": "tool_use",
                "id": "tool_1",
                "name": "write_file",
                "input": {
                    "path": "hello.py",
                    "content": "print('Hello, World!')"
                }
            }
        ],
        "stop_reason": "tool_use",
    },
    "fix the bug": {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I found and fixed the bug."},
            {
                "type": "tool_use",
                "id": "tool_1",
                "name": "edit_file",
                "input": {
                    "path": "main.py",
                    "old_string": "bug",
                    "new_string": "fix"
                }
            }
        ],
        "stop_reason": "tool_use",
    },
    "default": {
        "role": "assistant",
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
    },
}


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()

    # Extract user prompt from messages
    prompt = _extract_prompt(body.get("messages", []))

    # Find matching response
    response = _find_response(prompt)

    return {
        "id": "msg_test_123",
        "type": "message",
        "role": response["role"],
        "content": response["content"],
        "model": body.get("model", "claude-sonnet-4-20250514"),
        "stop_reason": response.get("stop_reason", "end_turn"),
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _extract_prompt(messages: list[dict]) -> str:
    """Extract text from last user message."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content.lower()
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text":
                        return block.get("text", "").lower()
    return ""


def _find_response(prompt: str) -> dict:
    """Find scripted response matching prompt."""
    for pattern, response in RESPONSES.items():
        if pattern == "default":
            continue
        if pattern.lower() in prompt:
            return response
    return RESPONSES["default"]
```

### 2.4 Docker Compose for Tests

```yaml
# docker-compose.test.yml

services:
  mock-anthropic:
    build:
      context: .
      dockerfile: tests/fixtures/Dockerfile.mock-anthropic
    ports:
      - "8000:8000"
    healthcheck:
      test: curl -f http://localhost:8000/health
      interval: 5s
      timeout: 3s
      retries: 3

  worker-test:
    build:
      context: .
      dockerfile: services/worker-wrapper/Dockerfile.test
    environment:
      ANTHROPIC_BASE_URL: http://mock-anthropic:8000
      ANTHROPIC_AUTH_TOKEN: test-key
      REDIS_URL: redis://redis-test:6379
    depends_on:
      mock-anthropic:
        condition: service_healthy
      redis-test:
        condition: service_healthy
```

### 2.5 Adding New Scripted Responses

Responses can be extended for specific test scenarios:

```python
# tests/integration/test_developer_worker.py

@pytest.fixture
def mock_anthropic_responses():
    """Custom responses for this test module."""
    return {
        "implement user authentication": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Implementing auth..."},
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "auth.py", "content": "..."}
                },
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "tests/test_auth.py", "content": "..."}
                },
            ],
            "stop_reason": "tool_use",
        },
    }

async def test_developer_creates_auth(mock_anthropic_responses):
    # Mock server uses these responses
    ...
```

## 3. Direct LLM API Mocking

For LangGraph nodes that call LLM directly (not via CLI agent).

### 3.1 Dependency Injection

```python
# services/langgraph/src/nodes/analyst.py

from langchain_openai import ChatOpenAI
from langchain_core.language_models import BaseChatModel

class AnalystNode:
    def __init__(self, llm: BaseChatModel | None = None):
        self.llm = llm or ChatOpenAI(model="gpt-4")

    async def run(self, state: State) -> State:
        response = await self.llm.ainvoke(state.messages)
        return {"analysis": response.content}
```

### 3.2 FakeLLM for Tests

```python
# tests/fixtures/fake_llm.py

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

class FakeLLM(BaseChatModel):
    """Deterministic LLM for testing."""

    responses: dict[str, str] = {}
    default_response: str = "OK"
    calls: list[list[BaseMessage]] = []

    def _generate(self, messages, **kwargs):
        self.calls.append(messages)

        # Find matching response
        last_human = self._get_last_human_message(messages)
        for pattern, response in self.responses.items():
            if pattern.lower() in last_human.lower():
                return self._make_result(response)

        return self._make_result(self.default_response)

    async def _agenerate(self, messages, **kwargs):
        return self._generate(messages, **kwargs)

    def _get_last_human_message(self, messages) -> str:
        for msg in reversed(messages):
            if msg.type == "human":
                return msg.content
        return ""

    def _make_result(self, content: str):
        return ChatResult(generations=[
            ChatGeneration(message=AIMessage(content=content))
        ])

    @property
    def _llm_type(self) -> str:
        return "fake"
```

### 3.3 Usage in Tests

```python
# tests/unit/test_analyst_node.py

from tests.fixtures.fake_llm import FakeLLM

async def test_analyst_analyzes_spec():
    fake_llm = FakeLLM(responses={
        "analyze this spec": "The spec looks complete. Key entities: User, Project.",
    })

    node = AnalystNode(llm=fake_llm)

    result = await node.run({
        "messages": [HumanMessage(content="Analyze this spec: ...")]
    })

    assert "Key entities" in result["analysis"]
    assert len(fake_llm.calls) == 1
```

## 4. Test Levels Summary

| Level | CLI Agent | Direct LLM | Cost |
|-------|-----------|------------|------|
| **Unit** | Mock subprocess | FakeLLM via DI | $0 |
| **Integration** | Mock Anthropic Server | FakeLLM | $0 |
| **E2E** | Real Claude API | Real LLM API | $$$ |

## 5. E2E with Real LLM

### 5.1 When to Use

- Nightly/weekly, not on every PR
- Smoke tests only (1-2 simple scenarios)
- Validating prompt quality, not code logic

### 5.2 What We're Testing

| Integration Tests (mock) | E2E Tests (real) |
|-------------------------|------------------|
| Worker wrapper works | Claude understands our prompts |
| Redis communication | Prompt templates effective |
| File operations | Real tool calls succeed |
| Error handling | End-to-end flow works |

### 5.3 Cost Estimation

| Scenario | ~Tokens | ~Cost |
|----------|---------|-------|
| Simple file create | 1K | $0.01 |
| Bug fix with context | 5K | $0.05 |
| Full feature implementation | 20K | $0.20 |

**Budget:** ~$10-20/month for nightly E2E tests.

### 5.4 E2E Test Example

```python
# tests/e2e/test_developer_real.py

import pytest

@pytest.mark.e2e
@pytest.mark.real_llm
async def test_developer_creates_simple_file():
    """
    E2E: Real Claude creates a real file.

    Cost: ~$0.02
    Run: nightly only
    """
    # 1. Setup: create temp repo in project-factory-test
    repo = await github.create_repo(f"e2e-{uuid()}")

    try:
        # 2. Send task to real worker (real Claude)
        await redis.xadd("worker:developer:input", {
            "task_id": "test-123",
            "prompt": "Create a file called hello.py that prints 'Hello E2E'",
            "timeout": 60,
        })

        # 3. Wait for result
        result = await wait_for_result("worker:developer:output", timeout=60)

        assert result["status"] == "success"

        # 4. Verify file in repo
        file = await github.get_file(repo.name, "hello.py")
        assert "Hello E2E" in file.content

    finally:
        # 5. Cleanup
        await github.delete_repo(repo.name)
```

## 6. Configuration

### 6.1 Environment Variables

| Variable | Unit/Integration | E2E |
|----------|-----------------|-----|
| `ANTHROPIC_BASE_URL` | `http://mock-anthropic:8000` | `https://api.anthropic.com` |
| `ANTHROPIC_API_KEY` | `test-key` | Real key |
| `GITHUB_ORG` | N/A (mock) | `project-factory-test` |

### 6.2 pytest Markers

```python
# pytest.ini

[pytest]
markers =
    unit: Fast tests, no external deps
    integration: Requires mock servers
    e2e: Full pipeline, real APIs
    real_llm: Uses real LLM API (costly)
```

### 6.3 Running Tests

```bash
# Unit + Integration (every PR)
make test-ci

# E2E with real LLM (nightly)
make test-e2e

# Skip costly tests
pytest -m "not real_llm"
```
