# Multi-Tenancy & Access Control Implementation Plan

## Проблема

Текущая архитектура **не разделяет данные между пользователями**:
- ❌ Любой пользователь Telegram может создавать проекты
- ❌ Проекты не привязаны к owner (нет поля `owner_id`)
- ❌ API `/api/projects` возвращает ВСЕ проекты всем пользователям
- ❌ Обычные пользователи могут видеть список серверов (инфраструктурные данные)
- ❌ RAG уже имеет user_id, но нет проверки на frontend

## Целевая Архитектура

### Уровни доступа
1. **Anonymous** - блокируется whitelist'ом
2. **User** (обычный пользователь):
   - Видит только свои проекты
   - Может создавать новые проекты (они автоматически становятся его)
   - **НЕ может** видеть список серверов
   - RAG видит только `scope=user` и `scope=public` данные
3. **Admin** (администратор):
   - Видит все проекты
   - Может управлять серверами (`/api/servers`)
   - Доступ к инфраструктурным операциям

### Thread Management
**Упрощение**: Отказываемся от концепции отдельных тредов.
- Каждый пользователь имеет **один непрерывный conversation thread**
- `thread_id` для LangGraph checkpointing = `f"user_{telegram_id}"`
- Вся история переписки пользователя хранится в одном thread
- Это упрощает архитектуру и делает поведение более предсказуемым

### User ID Nomenclature  
**Важно**: В системе есть два типа user_id:

1. **`telegram_id`** (или `telegram_user_id`) - это ID пользователя в Telegram (тип: `int`, например `123456789`)
   - Приходит от Telegram API в `update.effective_user.id`
   - Используется для whitelist проверки
   - Передаётся в Redis как `user_id` (исторические причины)
   - Используется в `thread_id = f"user_{telegram_id}"`
   
2. **`user.id`** (или `internal_user_id`) - это автоинкрементный ID в таблице `users` (тип: `int`, например `42`)
   - Первичный ключ таблицы `users`
   - Используется для foreign keys (`owner_id`, `user_id` в RAG)
   - Резолвится из `telegram_id` через API `/api/users/by-telegram/{telegram_id}`

**В коде**: Везде явно указываем `telegram_id` или `internal_user_id` для ясности.

## User Review Required

> [!IMPORTANT]
> **Backward Compatibility**: Существующие проекты в БД не имеют `owner_id`. Нужно решить:
> - Назначить всех существующих проектов себе (вашему telegram_id)?
> - Или оставить их без owner (null), и они будут видны только админам?

> [!WARNING]
> **Breaking Change**: После внедрения whitelist другие пользователи не смогут писать боту без добавления их telegram_id в `ALLOWED_TELEGRAM_IDS`.

## Proposed Changes

### Database Schema (✅ Completed)

#### [MODIFY] [project.py](file:///home/vlad/projects/codegen_orchestrator/shared/models/project.py)

Добавить поле `owner_id`:
```python
owner_id: Mapped[int | None] = mapped_column(
    Integer, ForeignKey("users.id"), nullable=True, index=True
)
```

**Rationale**: `nullable=True` для совместимости с существующими проектами. В будущем можем сделать NOT NULL.

#### [NEW] Alembic Migration

Создать миграцию `add_project_owner`:
```python
def upgrade():
    op.add_column('projects', 
        sa.Column('owner_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_projects_owner_id_users', 
        'projects', 'users',
        ['owner_id'], ['id']
    )
    op.create_index('ix_projects_owner_id', 'projects', ['owner_id'])
```

---

### Telegram Bot - User Authentication (✅ Completed)

#### [MODIFY] [config.py](file:///home/vlad/projects/codegen_orchestrator/services/telegram_bot/src/config.py)

Добавить поле для whitelist:
```python
class Settings(BaseSettings):
    # ... existing fields ...
    
    # Comma-separated list of allowed Telegram user IDs
    allowed_telegram_ids: str = Field(default="", env="ALLOWED_TELEGRAM_IDS")
    
    def get_allowed_ids(self) -> set[int]:
        """Parse comma-separated IDs into set of integers."""
        if not self.allowed_telegram_ids:
            return set()
        return {
            int(id.strip()) 
            for id in self.allowed_telegram_ids.split(",") 
            if id.strip().isdigit()
        }
```

#### [NEW] [middleware.py](file:///home/vlad/projects/codegen_orchestrator/services/telegram_bot/src/middleware.py)

Создать middleware для проверки доступа:
```python
"""Telegram bot middleware for user authentication."""
from telegram import Update
from telegram.ext import BaseHandler, ContextTypes
import structlog

from .config import get_settings

logger = structlog.get_logger()

async def auth_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is allowed to interact with bot.
    
    Returns True if user is whitelisted, False otherwise.
    Sends friendly rejection message for unauthorized users.
    """
    if not update.effective_user:
        return True  # System updates
    
    settings = get_settings()
    allowed_ids = settings.get_allowed_ids()
    
    # If whitelist is empty, allow all (backward compat)
    if not allowed_ids:
        logger.warning("no_whitelist_configured")
        return True
    
    user_id = update.effective_user.id
    
    if user_id in allowed_ids:
        return True
    
    # User not authorized
    logger.warning("unauthorized_access_attempt", 
                  telegram_id=user_id,
                  username=update.effective_user.username)
    
    if update.message:
        await update.message.reply_text(
            "🚫 Доступ запрещён.\n\n"
            "Этот бот доступен только авторизованным пользователям."
        )
    elif update.callback_query:
        await update.callback_query.answer(
            "🚫 Доступ запрещён",
            show_alert=True
        )
    
    return False
```

#### [MODIFY] [main.py](file:///home/vlad/projects/codegen_orchestrator/services/telegram_bot/src/main.py)

Обновить функцию `handle_message` для регистрации/обновления пользователя:
```python
async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - publish to Redis Stream."""
    # Auth check
    if not await auth_middleware(update, context):
        return
    
    telegram_user_id = update.effective_user.id  # Telegram user ID
    # ... existing code ...
    
    # Register/update user in database
    await _ensure_user_registered(update.effective_user)
    
    # Publish to Redis (NO thread_id needed - worker will create it)
    await redis_client.publish(
        RedisStreamClient.INCOMING_STREAM,
        {
            "user_id": telegram_user_id,  # Telegram ID (NOT internal DB id)
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            # REMOVED: "thread_id" - worker creates it as f"user_{telegram_user_id}"
            "correlation_id": correlation_id,
        },
    )
    # ...

async def _ensure_user_registered(tg_user) -> None:
    """Upsert user in database via API."""
    settings = get_settings()
    payload = {
        "telegram_id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "last_name": tg_user.last_name,
    }
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Upsert endpoint (создать в API)
            await client.post(
                f"{settings.api_url}/api/users/upsert",
                json=payload
            )
    except httpx.HTTPError as e:
        logger.warning("user_registration_failed", error=str(e))
```

Добавить middleware в обработчики:
```python
# Add middleware to all handlers
app.add_handler(MessageHandler(
    filters.ALL, 
    lambda u, c: auth_middleware(u, c)
), 
group=-1)  # Run before other handlers
```

---

### API - User Management

#### [NEW] [users.py](file:///home/vlad/projects/codegen_orchestrator/services/api/src/routers/users.py) - Add endpoints

**Upsert endpoint**:
```python
@router.post("/upsert", response_model=UserRead)
async def upsert_user(
    user_in: UserUpsert,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Create or update user by telegram_id."""
    query = select(User).where(User.telegram_id == user_in.telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if user:
        user.username = user_in.username
        user.first_name = user_in.first_name
        user.last_name = user_in.last_name
        user.last_seen = datetime.utcnow()
    else:
        user = User(
            telegram_id=user_in.telegram_id,
            username=user_in.username,
            first_name=user_in.first_name,
            last_name=user_in.last_name,
        )
        db.add(user)
    
    await db.commit()
    await db.refresh(user)
    return user
```

**Get by telegram_id endpoint** (for LangGraph worker):
```python
@router.get("/by-telegram/{telegram_id}", response_model=UserRead)
async def get_user_by_telegram_id(
    telegram_id: int,
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Get user by telegram_id (for internal services)."""
    query = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=404, 
            detail=f"User with telegram_id {telegram_id} not found"
        )
    
    return user
```


#### [NEW] [dependencies.py](file:///home/vlad/projects/codegen_orchestrator/services/api/src/dependencies.py)

Создать dependencies для авторизации:
```python
"""FastAPI dependencies for authorization."""
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import User
from .database import get_async_session

async def get_current_user(
    x_telegram_id: int = Header(...),
    db: AsyncSession = Depends(get_async_session),
) -> User:
    """Get current user from telegram_id header.
    
    Raises 401 if header missing, 404 if user not found.
    """
    query = select(User).where(User.telegram_id == x_telegram_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with telegram_id {x_telegram_id} not found"
        )
    
    return user

async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require user to be admin.
    
    Raises 403 if user is not admin.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return user
```

---

### API - Project Authorization

#### [MODIFY] [projects.py](file:///home/vlad/projects/codegen_orchestrator/services/api/src/routers/projects.py)

**Изменение 1**: Добавить header для `telegram_id` в создание проектов:
```python
@router.post("/", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    project_in: ProjectCreate,
    x_telegram_id: int = Header(None),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Create a new project."""
    # Resolve owner
    owner_id = None
    if x_telegram_id:
        query = select(User).where(User.telegram_id == x_telegram_id)
        result = await db.execute(query)
        user = result.scalar_one_or_none()
        if user:
            owner_id = user.id
    
    project = Project(
        id=project_in.id,
        name=project_in.name,
        status=project_in.status,
        config=project_in.config,
        owner_id=owner_id,  # NEW
    )
    # ... rest
```

**Изменение 2**: Фильтровать проекты по owner:
```python
@router.get("/", response_model=list[ProjectRead])
async def list_projects(
    status: str | None = None,
    x_telegram_id: int = Header(None),
    db: AsyncSession = Depends(get_async_session),
) -> list[Project]:
    """List projects for current user."""
    query = select(Project)
    
    # Filter by owner if telegram_id provided
    if x_telegram_id:
        # Resolve user
        user_query = select(User).where(User.telegram_id == x_telegram_id)
        user_result = await db.execute(user_query)
        user = user_result.scalar_one_or_none()
        
        if user:
            if not user.is_admin:
                # Regular user: only their projects
                query = query.where(Project.owner_id == user.id)
            # Admin: see all projects (no filter)
    
    if status:
        query = query.where(Project.status == status)
    
    result = await db.execute(query)
    return list(result.scalars().all())
```

**Изменение 3**: Проверить ownership на get/update:
```python
@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: str,
    x_telegram_id: int = Header(None),
    db: AsyncSession = Depends(get_async_session),
) -> Project:
    """Get project by ID."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Check ownership
    if x_telegram_id:
        user_query = select(User).where(User.telegram_id == x_telegram_id)
        user_result = await db.execute(user_query)
        user = user_result.scalar_one_or_none()
        
        if user and not user.is_admin:
            if project.owner_id != user.id:
                raise HTTPException(
                    status_code=403,
                    detail="Access denied: not project owner"
                )
    
    return project
```

---

### API - Admin Endpoints

#### [MODIFY] [servers.py](file:///home/vlad/projects/codegen_orchestrator/services/api/src/routers/servers.py)

Защитить эндпоинты:
```python
from ..dependencies import require_admin

@router.get("/", response_model=list[ServerRead])
async def list_servers(
    is_managed: bool | None = None,
    admin: User = Depends(require_admin),  # NEW
    db: AsyncSession = Depends(get_async_session),
) -> list[Server]:
    """List servers (admin only)."""
    # ... existing code ...
```

---

### Telegram Handlers - Pass User Context

#### [MODIFY] [handlers.py](file:///home/vlad/projects/codegen_orchestrator/services/telegram_bot/src/handlers.py)

Добавить `X-Telegram-ID` header в API вызовы:
```python
async def _api_get(path: str, telegram_id: int | None = None) -> dict | list | None:
    """Make GET request to API service."""
    settings = get_settings()
    url = f"{settings.api_url}{path}"
    
    headers = {}
    if telegram_id:
        headers["X-Telegram-ID"] = str(telegram_id)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as e:
        logger.error("api_request_failed", url=url, error=str(e))
        return None
```

Обновить вызовы:
```python
async def _handle_projects(query, parts: list[str]) -> None:
    telegram_id = query.from_user.id
    projects = await _api_get("/api/projects", telegram_id=telegram_id)
    # ...

async def _handle_servers(query, parts: list[str]) -> None:
    telegram_id = query.from_user.id
    servers = await _api_get("/api/servers?is_managed=true", telegram_id=telegram_id)
    # ...
```

---

### LangGraph - User Propagation

#### [MODIFY] [worker.py](file:///home/vlad/projects/codegen_orchestrator/services/langgraph/src/worker.py)

**Изменение 1**: Упростить thread_id - всегда использовать `user_{telegram_id}`:
```python
async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    """Process a single message through the LangGraph."""
    user_id = data.get("user_id")  # This is telegram_id
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    correlation_id = data.get("correlation_id")
    
    # SIMPLIFIED: Always use user_{telegram_id} as thread_id
    thread_id = f"user_{user_id}"
    
    # Resolve internal user_id from telegram_id
    internal_user_id = await _resolve_user_id(user_id)
    
    # Bind request context
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id, 
        correlation_id=correlation_id, 
        telegram_user_id=user_id,
        user_id=internal_user_id,
    )
    
    # ... existing history logic ...
    
    state: OrchestratorState = {
        "messages": list(history),
        # ... existing fields ...
        "telegram_user_id": user_id,  # NEW
        "user_id": internal_user_id,  # NEW
    }
    
    # LangGraph config - thread_id is always user-based
    config = {"configurable": {"thread_id": thread_id}}
    # ...
```

**Изменение 2**: Добавить helper для резолвинга user_id:
```python
async def _resolve_user_id(telegram_id: int) -> int | None:
    """Resolve internal user.id from telegram_id via API.
    
    Returns None if user not found or API error.
    """
    settings = get_settings()
    url = f"{settings.api_url}/api/users/by-telegram/{telegram_id}"
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                user_data = response.json()
                return user_data.get("id")
    except Exception as e:
        logger.warning("user_id_resolution_failed", 
                      telegram_id=telegram_id, 
                      error=str(e))
    return None
```


#### [MODIFY] [graph.py](file:///home/vlad/projects/codegen_orchestrator/services/langgraph/src/graph.py)

Добавить в `OrchestratorState`:
```python
class OrchestratorState(TypedDict):
    # ... existing fields ...
    
    # User context
    telegram_user_id: int | None
    user_id: int | None  # Internal database user.id
```

#### [MODIFY] Analyst create_project tool

В `services/langgraph/src/tools/project.py`:
```python
async def create_project(..., state: OrchestratorState):
    payload = {
        "id": project_id,
        "name": name,
        "status": "draft",
        "config": {...},
    }
    
    headers = {}
    if state.get("telegram_user_id"):
        headers["X-Telegram-ID"] = str(state["telegram_user_id"])
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_URL}/api/projects",
            json=payload,
            headers=headers,  # NEW
        )
```

#### [MODIFY] RAG tool

Убедиться что `user_id` передаётся:
```python
# In services/langgraph/src/tools/rag.py
async def search_project_context(..., state: OrchestratorState):
    payload = {
        "query": query,
        "scope": "project",
        "user_id": state.get("user_id"),  # Use internal user.id
        "project_id": state.get("current_project"),
        # ...
    }
```

---

## Verification Plan

### Phase 1: Database & Models
```bash
# Generate migration
cd /home/vlad/projects/codegen_orchestrator
make shell-tooling
cd /workspace
alembic revision --autogenerate -m "Add project owner_id"

# Apply migration
make migrate

# Verify schema
make shell-api
psql $DATABASE_URL -c "\d projects"
```

### Phase 2: Telegram Whitelist
1. Add your telegram_id to `.env`:
   ```bash
   ALLOWED_TELEGRAM_IDS=YOUR_TELEGRAM_ID
   ```
2. Restart telegram_bot service
3. Try messaging bot from another account → should be blocked
4. Message from your account → should work

### Phase 3: Project Isolation
1. Create project as user A
2. Try to list projects via API with user B's telegram_id header
3. Verify user B doesn't see user A's projects
4. Verify admin sees all projects

### Phase 4: Admin Endpoints
1. Call `/api/servers` without header → 401
2. Call with regular user header → 403
3. Call with admin user header → 200 OK
4. Promote user to admin:
   ```sql
   UPDATE users SET is_admin = true WHERE telegram_id = YOUR_ID;
   ```

### Phase 5: End-to-End
1. User creates project via Telegram
2. Verify `owner_id` set correctly in DB
3. User queries "найди информацию о проекте"
4. Verify RAG returns only their data
5. Verify LangGraph has access to user's projects only
