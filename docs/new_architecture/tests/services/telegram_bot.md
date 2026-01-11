# Testing Strategy: Telegram Bot

This document defines the testing strategy for the `telegram-bot` service.
The bot sits at the intersection of User interactions, API calls, and Redis message passing. We use **Component Tests** where the bot logic is real, but external dependencies (Telegram Server, API Service) are mocked, while Redis remains real to ensure correct async message handling.

## 1. Philosophy: Event-Driven Verification

We treat the bot as a reactive system:
*   **Input**: User Message (Standard or Button) OR Redis Event (Worker output, Provisioning result).
*   **Process**: Route handling, State check, Logic execution.
*   **Output**: Telegram Message (sent to user) OR Redis Message (sent to worker) OR API Request.

> **Key Rule**: We do NOT mock `aiogram` internals if possible, but rather feed it constructed `Update` objects. We DO mock the network layer that `aiogram` uses to send messages back, so we can assert "Bot sent X text to user Y".

## 2. Test Pyramid

| Level | Scope | Focus | Implementation |
|-------|-------|-------|----------------|
| **Component** | Bot + Redis | Message Routing, Session Logic, Queue Listening | `pytest-asyncio`, `respx` (API mocks), `aiogram.types.Update` |
| **Unit** | Utils | Text formatters, Data parsers | Pure Python |

## 3. Test Infrastructure & Fixtures

### 3.1 `redis_client` (Real)
*   Standard Redis fixture (same as other services) to verify:
    *   Session keys created (`session:{user_id}`).
    *   Messages pushed to `worker:commands` or `worker:po:{worker_id}:input`.
    *   Listening to `provisioner:results`.

### 3.2 `api_mock` (respx)
*   Intercepts HTTP calls to `http://api:8000`.
*   We define predefined responses for:
    *   `GET /api/users` (Auth check).
    *   `GET /api/projects` (Command execution).

### 3.3 `bot_harness`
A helper class to:
1.  **Simulate Incoming Message**: Synthesize an `aiogram.types.Update` and push it into the Dispatcher.
2.  **Capture Outgoing Message**: Mock the `bot.send_message` method to capture calls for assertion.

## 4. Test Scenarios

### 4.1 Authentication & Whitelist
*   **Scenario Refused**:
    *   `api_mock` configured to return 404 for User ID `999`.
    *   Simulate `/start` from ID `999`.
    *   **Assert**: Bot replies "Access denied".
*   **Scenario Allowed**:
    *   `api_mock` returns User JSON for ID `123`.
    *   Simulate `/start`.
    *   **Assert**: Bot replies "Welcome".

### 4.2 Quick Commands (API Interaction)
Verify buttons work without spinning up a worker.

*   **Scenario**: User clicks "My Projects".
*   **Action**: Simulate `/projects` command.
*   **Mock**: `api_mock` expects `GET /api/projects` and returns `[{"name": "foo"}]`.
*   **Assert**: Bot replies with a formatted list containing "foo".

### 4.3 PO Session & Message Relay
Verify the core chat loop.

*   **Step 1: New Session**:
    *   User `123` sends "Hello".
    *   **Assert**: Bot checks Redis `session:123`. Not found.
    *   **Assert**: Bot publishes `CreateWorkerCommand` to `worker:commands`.
    *   **Assert**: Bot saves `session:123` -> `po-worker-123`.

*   **Step 2: Message Forwarding**:
    *   User `123` sends "Build app".
    *   **Assert**: Bot publishes "Build app" to `worker:po:po-worker-123:input`.

*   **Step 3: Worker Reply**:
    *   **Trigger**: Inject message into Redis `worker:po:po-worker-123:output`.
    *   **Assert**: Bot listener picks it up and calls `send_message(chat_id=123, text="Building...")`.

### 4.4 Admin Notifications
Verify the bot monitors system-wide events.

*   **Trigger**: Publish `ProvisionerResult(status="success", server="srv-1")` to `provisioner:results`.
*   **Assert**: Bot sends message to Admin IDs (configured in env/mock).
*   **Assert**: Non-admins do NOT receive this.

## 5. Implementation Notes

*   Use `respx` for robust HTTP mocking.
*   The `Bot` instance often needs to be initialized without actually calling `start_polling` (which blocks). We manually feed updates via `await dispatcher.feed_update(bot, update)`.
