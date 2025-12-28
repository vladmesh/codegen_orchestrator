"""AGENTS.md template for Developer agent.

This file defines the framework conventions and patterns that the Developer
agent should follow when working on the project.
"""

AGENTS_MD_TEMPLATE = """# AGENTS.md - Framework Guide for AI Developers

This document provides guidelines for AI agents working on this codebase.

## Project Structure

```
{project_name}/
├── services/              # Microservices
│   ├── backend/          # FastAPI REST API (if included)
│   │   ├── src/
│   │   │   ├── generated/    # Auto-generated (DO NOT EDIT)
│   │   │   ├── controllers/  # Business logic (IMPLEMENT HERE)
│   │   │   └── app/          # Custom application code
│   │   └── spec/             # Domain specifications
│   └── tg_bot/           # Telegram bot (if included)
├── shared/
│   ├── spec/             # Shared specifications
│   │   ├── models.yaml   # Data models definition
│   │   └── events.yaml   # Event definitions (optional)
│   └── shared/
│       └── generated/    # Generated schemas (DO NOT EDIT)
├── infra/                # Docker Compose files
├── Makefile              # Build commands
└── TASK.md               # Your current task
```

## Workflow

### 1. Spec-First Development

All code generation starts with YAML specs. Never write boilerplate manually.

**Models** (`shared/spec/models.yaml`):
```yaml
models:
  User:
    fields:
      id:
        type: int
        readonly: true
      email:
        type: str
      name:
        type: str
      created_at:
        type: datetime
        readonly: true
    variants:
      Create: {{}}          # Excludes readonly fields
      Update:
        optional: [name]   # Makes fields optional
      Read: {{}}            # Includes all fields
```

**Domain Specs** (`services/backend/spec/users.yaml`):
```yaml
domain: users
config:
  rest:
    prefix: "/users"
    tags: ["users"]

operations:
  create_user:
    input: UserCreate
    output: UserRead
    rest:
      method: POST
      path: ""
      status: 201

  get_user:
    output: UserRead
    params:
      - name: user_id
        type: int
    rest:
      method: GET
      path: "/{{user_id}}"
```

### 2. Code Generation

After modifying specs, always run:
```bash
make generate-from-spec
```

This generates:
- `shared/shared/generated/schemas.py` - Pydantic models
- `services/*/src/generated/routers/*.py` - FastAPI routers
- `services/*/src/generated/protocols.py` - Controller interfaces

### 3. Implement Controllers

Controllers contain business logic. Follow the generated Protocol:

```python
# services/backend/src/controllers/users.py
from shared.shared.generated.schemas import UserCreate, UserRead
from sqlalchemy.ext.asyncio import AsyncSession
from src.generated.protocols import UsersControllerProtocol

class UsersController(UsersControllerProtocol):
    async def create_user(
        self,
        session: AsyncSession,
        payload: UserCreate,
    ) -> UserRead:
        # Implement business logic here
        user = User(**payload.model_dump())
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return UserRead.model_validate(user)
```

## Key Commands

```bash
make generate-from-spec   # Generate code from YAML specs
make lint                 # Check code quality
make format               # Auto-format code
make test                 # Run all tests
make dev-start            # Start services locally
```

## Important Rules

1. **Never edit `src/generated/`** - These files are auto-generated
2. **Spec first** - Always modify specs, then regenerate
3. **Async everywhere** - All I/O operations must be async
4. **Type hints** - Use proper type annotations
5. **Logging** - Use structlog: `logger.info("event", key=value)`

## Selected Modules

This project includes: **{modules}**

{module_specific_notes}
"""

MODULE_NOTES = {
    "backend": """### Backend Module
- FastAPI REST API on port 8000
- PostgreSQL database with SQLAlchemy 2.0
- Alembic for migrations
- Health check at `/health`
""",
    "tg_bot": """### Telegram Bot Module
- python-telegram-bot framework
- Event-driven via FastStream + Redis
- Subscribes to events from backend
""",
    "notifications": """### Notifications Module
- Background worker for notifications
- Email and Telegram notification support
- Event-driven via FastStream + Redis
""",
    "frontend": """### Frontend Module
- Node.js frontend on port 4321
- API client generated from OpenAPI spec
""",
}


def render_agents_md(
    project_name: str,
    modules: list[str],
) -> str:
    """Render AGENTS.md template with module-specific notes.

    Args:
        project_name: Name of the project
        modules: List of selected modules

    Returns:
        Rendered AGENTS.md content
    """
    module_notes_parts = []
    for module in modules:
        if module in MODULE_NOTES:
            module_notes_parts.append(MODULE_NOTES[module])

    module_specific_notes = "\n".join(module_notes_parts) if module_notes_parts else ""

    return AGENTS_MD_TEMPLATE.format(
        project_name=project_name,
        modules=", ".join(modules) if modules else "backend",
        module_specific_notes=module_specific_notes,
    )
