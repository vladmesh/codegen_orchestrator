# #1035 Telegram bot: dashboard button with one-time token

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Second half of LK auth flow. #1034 (done) built the server side: `POST /api/lk/auth/token` accepts a one-time Redis token and returns a JWT. This task adds the client side in the Telegram bot: generate the token, store in Redis, send URL to user.

**Current state**: Bot has handlers in `main.py`/`handlers.py`, keyboards in `keyboards.py`, Redis access via `RedisStreamClient` (`.redis` property for raw key-value), API client for user/project lookups. No `/dashboard` command exists yet. No `LK_DOMAIN` env var.

## Steps

1. [ ] Add LK_DOMAIN to bot config + /dashboard handler
   - **Input**: `services/telegram_bot/src/config.py`, `services/telegram_bot/src/main.py`, `services/telegram_bot/src/keyboards.py`
   - **Output**:
     - `lk_domain` field in Settings (required, e.g. `https://lk.codegen.example.com`)
     - New `dashboard()` handler for `/dashboard` command:
       1. Get user telegram_id → call API `GET /users/by-telegram/{tg_id}` → get `user_id`
       2. Check user owns projects: `GET /projects?owner_id=user_id` (or use X-Telegram-ID header). If none → reply "У вас пока нет проектов"
       3. Generate UUID token, `redis.set(f"lk_token:{token}", str(user_id), ex=300)`
       4. Send message with `InlineKeyboardButton("📊 Открыть дашборд", url=f"{lk_domain}/auth?token={token}")`
     - Register `CommandHandler("dashboard", dashboard)` in app
   - **Test**: Service test: mock Redis + API, call handler, verify Redis key set with correct TTL, verify reply contains URL button with token

2. [ ] Add dashboard button to main menu
   - **Input**: `services/telegram_bot/src/keyboards.py`, `services/telegram_bot/src/handlers.py`
   - **Output**:
     - Add "📊 Мой дашборд" button to `main_menu_keyboard()` (for all users, not just admins)
     - New callback prefix `PREFIX_DASHBOARD = "dashboard"` and handler in `handle_callback_query` that generates token and sends URL (same logic as /dashboard command)
   - **Test**: Service test: verify main menu keyboard contains dashboard button, verify callback handler generates token

3. [ ] Docker-compose + env var wiring
   - **Input**: `docker-compose.yml`, `.env`
   - **Output**: `LK_DOMAIN` env var passed to telegram_bot service. Added to `.env` with placeholder.
   - **Test**: `make up` — bot starts without crash

