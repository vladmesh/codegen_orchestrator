# Service: Telegram Bot

**Service Name:** `telegram-bot`
**Current Name:** `telegram_bot` (requires rename)
**Responsibility:** User Interface + PO Session Management.

## 1. Philosophy

The Telegram Bot is the **front door** for users. It handles authentication, routes messages to the Product Owner (PO) worker, and provides quick shortcuts for common actions.

> **Rule #1:** Unknown users are rejected (whitelist-based access).
> **Rule #2:** Each user gets their own PO Worker for interactive sessions.
> **Rule #3:** Simple queries go directly to API (no tokens wasted on PO).

## 2. Responsibilities

1.  **Authentication**: Check if user exists in DB, reject if not.
2.  **PO Session Management**: Create/retrieve PO Worker for user via `worker-manager`.
3.  **Message Relay**: Forward user messages to PO, relay PO responses back.
4.  **Admin Notifications**: Listen to `provisioner:results` and notify admins about server setup status.
5.  **Quick Actions**: Handle simple commands via API (no PO involvement).

## 3. User Flow

```
┌──────────────────────────────────────────────────────────────┐
│                        USER MESSAGE                          │
└─────────────────────────────┬────────────────────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  User in DB?     │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │ NO                          │ YES
              ▼                             ▼
      ┌───────────────┐           ┌──────────────────┐
      │ Reject (403)  │           │ Quick command?   │
      │ "Access denied│           │ (/projects, etc) │
      └───────────────┘           └────────┬─────────┘
                                           │
                            ┌──────────────┴──────────────┐
                            │ YES                         │ NO
                            ▼                             ▼
                    ┌───────────────┐           ┌──────────────────┐
                    │ Call API      │           │ Get/Create PO    │
                    │ Return result │           │ Forward message  │
                    └───────────────┘           └──────────────────┘
```

## 4. Features

### 4.1 Authentication
- On first message: `GET /api/users?telegram_id={id}`
- If not found → "Доступ запрещён. Обратитесь к администратору."

### 4.2 Quick Commands (Direct to API)
Buttons/commands that bypass PO to save tokens:

| Command | API Call | Response |
|---------|----------|----------|
| `/projects` | `GET /api/projects` | List of user's projects |
| `/servers` | `GET /api/servers` | Server statuses |
| `/tasks` | `GET /api/tasks?status=running` | Active tasks |
| `/help` | — | Help message (hardcoded) |

### 4.3 PO Session Management
- Session = mapping `user_id → worker_id` (stored in Redis)
- On first real message:
  1. Check Redis for existing `session:{user_id}`
  2. If exists → check `HGET worker:status:{id} status` (sync lookup)
  3. If status is `RUNNING` → reuse worker
  4. If status is `STOPPED`/`FAILED` or not exists → create new PO Worker via `worker:commands`
  5. Store `session:{user_id} = worker_id` (TTL: 24h)

### 4.4 Message Relay
- User → Bot: `XADD worker:po:{user_id}:input`
- PO → User: `XREAD worker:po:{user_id}:output` → send to Telegram

## 5. Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                       TELEGRAM BOT                            │
├───────────────────────────────────────────────────────────────┤
│                                                               │
│   handlers/                                                   │
│     ├── auth.py           User validation                     │
│     ├── commands.py       Quick commands (/projects, etc)     │
│     ├── chat.py           PO message relay                    │
│     └── notifications.py  Provisioning alerts (admin only)    │
│                                                               │
│   session_manager.py      PO Worker lifecycle                 │
│                                                               │
└───────────────────────────┬───────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
  ┌──────────┐       ┌──────────────┐    ┌──────────────┐
  │   API    │       │    Redis     │    │ Worker-Mgr   │
  │ (users,  │       │ (sessions,   │    │ (PO spawn)   │
  │ projects)│       │  streams)    │    │              │
  └──────────┘       └──────────────┘    └──────────────┘
```

## 6. Dependencies

**Allowed:**
*   `aiogram` / `python-telegram-bot`
*   `aiohttp` (API client)
*   `redis` (sessions, streams)
*   `structlog`

**BANNED:**
*   Direct DB access (use API)
*   LangGraph dependencies
*   Heavy ML libraries

## 7. PO Worker Configuration

When spawning PO, bot sends to `worker:commands`:

```python
CreateWorkerCommand(
    config=WorkerConfig(
        name=f"po-{user_id}",
        agent="claude",
        capabilities=[],
        env_vars={
            "USER_ID": str(user_id),
            "API_URL": "http://api:8000",
        },
    ),
    context={
        "role": "product_owner",
        "user_telegram_id": str(telegram_id),
    },
)
```
