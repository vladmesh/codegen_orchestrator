# Plan: Telegram "Add User" Button for Admins (#49)

## Context

Admins need a way to add new users to the system directly from Telegram bot UI.

**Current state:**
- Admin detection works: `ADMIN_TELEGRAM_IDS` env var + `is_admin` DB flag, checked via `middleware.py:is_admin()`
- `main_menu_keyboard()` already accepts `is_admin` param and conditionally shows admin buttons (e.g. servers list)
- API already has `POST /users/` (create) and `POST /users/upsert` endpoints in `services/api/src/routers/users.py`
- `TelegramAPIClient` has `post_json()` method for POST requests
- Callback handler in `handlers.py` dispatches by prefix (`menu`, `projects`, `servers`, `project`)

**What needs to change:**
1. Add "Add User" button to admin menu in `keyboards.py`
2. Add callback handler for the new prefix in `handlers.py`
3. Use ConversationHandler to collect telegram_id from admin, then call API to create user

**Design decision — ConversationHandler vs simple text input:**
ConversationHandler is cleaner (scoped state, timeout, cancel). But the bot currently uses a single `MessageHandler(filters.TEXT)` that sends everything to PO. Adding a ConversationHandler with higher priority (lower group) will intercept text during the conversation flow, then fall through to PO when not in conversation. This is the standard python-telegram-bot pattern.

## Steps

1. [ ] Add admin keyboard constants and button
   - **Input**: `services/telegram_bot/src/keyboards.py`
   - **Output**: New `PREFIX_ADMIN = "admin"`, `ACTION_ADD_USER = "add_user"` constants. `main_menu_keyboard()` shows "👤 Добавить пользователя" button when `is_admin=True`. Export new constants.
   - **Test**: Unit test `test_main_menu_keyboard_admin_shows_add_user` — verify button present when `is_admin=True`, absent when `False`.

2. [ ] Add admin callback handler (entry point)
   - **Input**: `services/telegram_bot/src/handlers.py`
   - **Output**: New `PREFIX_ADMIN` dispatch in `handle_callback_query`. `_handle_admin()` function that handles `admin:add_user` — replies with "Введите Telegram ID нового пользователя" and sets `context.user_data["awaiting_add_user"] = True`.
   - **Test**: Unit test `test_handle_admin_add_user_callback` — verify correct prompt message and user_data flag set.

3. [ ] Add text handler for receiving telegram_id and creating user
   - **Input**: `services/telegram_bot/src/handlers.py`, `services/telegram_bot/src/clients/api.py`
   - **Output**: New `handle_add_user_input()` function: checks `context.user_data.get("awaiting_add_user")`, validates input is numeric telegram_id, calls `POST /users/` via `api_client.post_json()`, reports success/failure, clears flag. New `handle_add_user_cancel()` for `/cancel` during flow.
   - **Test**: Unit test `test_handle_add_user_input_success` — mock API call, verify user created message. `test_handle_add_user_input_invalid` — non-numeric input. `test_handle_add_user_input_duplicate` — API returns 400.

4. [ ] Register handlers in main.py
   - **Input**: `services/telegram_bot/src/main.py`
   - **Output**: Import and register `handle_add_user_input` as a MessageHandler with a filter that checks `user_data["awaiting_add_user"]` flag, placed BEFORE the general text handler. Add `/cancel` command handler.
   - **Test**: Unit test `test_add_user_flow_integration` — simulate full flow: callback → text input → API call → response. Verify flag cleared after completion.

5. [ ] Wire up and verify exports
   - **Input**: `services/telegram_bot/src/handlers.py`, `services/telegram_bot/src/keyboards.py`
   - **Output**: All new constants exported from keyboards.py, all new handlers importable. Run `make test-telegram-unit` and `make lint`.
   - **Test**: `make test-telegram-unit` passes, `make lint` passes.
