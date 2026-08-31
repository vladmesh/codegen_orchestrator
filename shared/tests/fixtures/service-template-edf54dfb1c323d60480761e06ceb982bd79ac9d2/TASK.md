# Task: env_fixture

## Overview

No detailed task description provided.

## Project Configuration

- **Name**: env_fixture
- **Modules**: backend,tg_bot,notifications,frontend

## Implementation Guidelines

1. **Read the codebase structure** - Understand how the scaffolded project is organized
2. **Follow existing patterns** - Use the same code style and architecture
3. **Implement the requirements** - Build what's described above
4. **Write tests** - Add unit tests for new functionality
5. **Commit with clear messages** - Describe what was implemented

## Files to Focus On

### Backend
- `services/backend/src/app/` - Business logic
- `services/backend/src/controllers/` - API controllers
- `shared/spec/models.yaml` - Data models (run `make generate-from-spec` after changes)

### Telegram Bot
- `services/tg_bot/src/main.py` - Bot handlers and commands
- `services/tg_bot/src/handlers/` - Additional handlers (create if needed)

### Notifications
- `services/notifications_worker/src/` - Notification processing

### Frontend
- `services/frontend/src/` - Frontend application

## Commands

```bash
# Start development environment
make dev-start

# Run tests
make tests

# Generate code from specs
make generate-from-spec
# Format code
make format
```
