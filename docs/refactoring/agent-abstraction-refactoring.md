# Agent Abstraction Refactoring Plan

**Цель**: Устранить жёсткую привязку Telegram бота к Claude CLI и вынести все агент-специфичные детали в workers-spawner на уровень фабрик.

**Проблема**: `telegram_bot/src/agent_manager.py` напрямую формирует команды `claude -p "..." --resume session_id`, знает о формате JSON вывода, управляет session_id в Redis бота. Это делает невозможным безболезненную замену Claude на другого агента.

**Решение**: Реализовать высокоуровневый API `send_message(agent_id, text) -> text` в workers-spawner, переместить всю агент-специфичную логику в фабрики.

---

## Progress Tracking

| Фаза | Статус | Описание | Коммит |
|------|--------|----------|--------|
| 0 | ✅ Завершена | Анализ и планирование | - |
| 1-2 | ✅ Завершена | AgentFactory.send_message + SessionManager | d852a85 |
| 3 | ✅ Завершена | Redis handler для send_message | bd2b045 |
| 4-5 | ✅ Завершена | Telegram bot migration (без rollout) | 179f8ee |
| 6 | ⏸️ Отложена | Оптимизации (опционально) | - |

**Статус**: Фазы 0-5 завершены. Telegram бот полностью агностичен к типу агента.

---

## Целевая архитектура

### До (текущее состояние)

```
┌─────────────────────────────────────────────────────┐
│ Telegram Bot                                        │
│ ┌─────────────────────────────────────────────────┐ │
│ │ agent_manager.py                                │ │
│ │ • Формирует "claude -p '...' --resume sid"     │ │
│ │ • Управляет session_id в Redis                  │ │
│ │ • Парсит JSON: {"result": "...", "session_id"}  │ │
│ │ • Экранирует shell кавычки                      │ │
│ └─────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │ send_command(agent_id, "claude...")
                   ▼
┌─────────────────────────────────────────────────────┐
│ workers-spawner                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ redis_handlers.py                               │ │
│ │ • send_command → docker exec /bin/bash -c       │ │
│ └─────────────────────────────────────────────────┘ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ Фабрики (НЕ ИСПОЛЬЗУЮТСЯ ботом)                │ │
│ │ • ClaudeCodeAgent.get_agent_command()           │ │
│ │ • FactoryDroidAgent.get_agent_command()         │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Проблемы**:
- ❌ Бот знает про `claude` CLI
- ❌ Бот управляет session_id (Claude-специфичная логика)
- ❌ Бот парсит JSON формат Claude CLI
- ❌ Невозможно добавить Factory Droid без изменений в боте
- ❌ Фабрики созданы, но обходятся

### После (целевое состояние)

```
┌─────────────────────────────────────────────────────┐
│ Telegram Bot                                        │
│ ┌─────────────────────────────────────────────────┐ │
│ │ agent_manager.py                                │ │
│ │ • send_message(agent_id, text) -> text          │ │
│ │ • Никаких деталей про CLI/session/JSON          │ │
│ └─────────────────────────────────────────────────┘ │
└──────────────────┬──────────────────────────────────┘
                   │ send_message(agent_id, "Привет")
                   ▼
┌─────────────────────────────────────────────────────┐
│ workers-spawner                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ redis_handlers.py                               │ │
│ │ • send_message → делегирует в AgentSession      │ │
│ └──────────────────┬──────────────────────────────┘ │
│                    ▼                                │
│ ┌─────────────────────────────────────────────────┐ │
│ │ AgentSession (новый слой)                       │ │
│ │ • Управляет персистентной сессией               │ │
│ │ • Делегирует в фабрику агента                   │ │
│ └──────────────────┬──────────────────────────────┘ │
│                    ▼                                │
│ ┌─────────────────────────────────────────────────┐ │
│ │ AgentFactory.send_message()                     │ │
│ │ • ClaudeCodeAgent: управляет session_id,        │ │
│ │   формирует команду, парсит JSON                │ │
│ │ • FactoryDroidAgent: свой протокол              │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Преимущества**:
- ✅ Бот агностичен к типу агента
- ✅ Вся Claude-логика в `ClaudeCodeAgent`
- ✅ Легко добавить новых агентов
- ✅ Session management на правильном уровне
- ✅ Переиспользование фабрик

---

## Итеративный план выполнения

### Фаза 0: Подготовка и анализ

**Задачи**:
- [x] Анализ текущего кода (`agent_manager.py`, `workers_spawner`)
- [x] Выявление всех мест, где бот знает про Claude
- [x] Составление этого плана
- [ ] Review плана с командой

**Результат**: Утверждённый план рефакторинга

---

### Фаза 1: Расширение интерфейса AgentFactory

**Цель**: Добавить метод `send_message()` в базовый класс фабрики, не ломая существующий код.

#### Шаг 1.1: Расширить базовый класс AgentFactory

**Файл**: `services/workers-spawner/src/workers_spawner/factories/base.py`

**Изменения**:
```python
class AgentFactory(ABC):
    # ... существующие методы ...

    async def send_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict[str, Any]:
        """Send text message to agent, get structured response.

        Args:
            agent_id: Container ID
            message: User message text
            session_context: Optional session state (agent-specific)

        Returns:
            {
                "response": str,  # Agent's response text
                "session_context": dict | None,  # Updated session state
                "metadata": dict  # Agent-specific metadata
            }
        """
        raise NotImplementedError("Subclasses must implement send_message")
```

**Тесты**: `services/workers-spawner/tests/unit/test_agent_factory_interface.py`
- Проверить, что метод объявлен
- Проверить сигнатуру

**Критерий завершения**: Тесты проходят, существующий код работает (метод не вызывается)

---

#### Шаг 1.2: Реализовать send_message в ClaudeCodeAgent

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/claude_code.py`

**Изменения**:
```python
@register_agent(AgentType.CLAUDE_CODE)
class ClaudeCodeAgent(AgentFactory):
    # ... существующие методы ...

    async def send_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict[str, Any]:
        """Send message to Claude CLI and parse response.

        Session management:
        - session_context = {"session_id": "sid-xxx"}
        - Первый запрос: session_id = None, Claude создаст новую сессию
        - Последующие: используем session_id из предыдущего ответа
        """
        from workers_spawner.container_service import ContainerService

        # Получаем container_service (инъекция зависимости или глобальный)
        # TODO: Рефакторинг для правильной DI
        container_service = ContainerService()

        session_id = session_context.get("session_id") if session_context else None

        # Формируем команду (логика из agent_manager.py)
        safe_message = message.replace("'", "'\\''")

        cmd_parts = [
            "claude",
            "--dangerously-skip-permissions",
            "-p", f"'{safe_message}'",
            "--output-format", "json",
        ]

        if session_id:
            cmd_parts.extend(["--resume", session_id])

        full_command = " ".join(cmd_parts)

        # Выполнение
        result = await container_service.send_command(
            agent_id,
            full_command,
            timeout=120
        )

        # Парсинг ответа
        try:
            data = json.loads(result.output)
            response_text = data.get("result", "")
            new_session_id = data.get("session_id")

            return {
                "response": response_text,
                "session_context": {"session_id": new_session_id} if new_session_id else None,
                "metadata": {
                    "exit_code": result.exit_code,
                    "success": result.success,
                }
            }
        except json.JSONDecodeError:
            # Fallback для нестандартного вывода
            return {
                "response": result.output,
                "session_context": session_context,  # Сохраняем старый контекст
                "metadata": {"parse_error": True}
            }
```

**Тесты**: `services/workers-spawner/tests/unit/test_claude_agent_send_message.py`
- Мокировать `ContainerService.send_command`
- Тест успешного JSON ответа
- Тест fallback при ошибке парсинга
- Тест с session_id и без

**Критерий завершения**: Юнит-тесты проходят, метод протестирован изолированно

---

#### Шаг 1.3: Реализовать заглушку для FactoryDroidAgent

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/factory_droid.py`

**Изменения**:
```python
@register_agent(AgentType.FACTORY_DROID)
class FactoryDroidAgent(AgentFactory):
    # ... существующие методы ...

    async def send_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict[str, Any]:
        """Send message to Factory Droid CLI.

        TODO: Реализовать после изучения протокола Factory Droid.
        Пока возвращаем заглушку.
        """
        raise NotImplementedError(
            "Factory Droid send_message not yet implemented. "
            "See https://docs.factory.ai/cli for protocol details."
        )
```

**Тесты**: Проверить, что выбрасывается NotImplementedError

**Критерий завершения**: Заглушка работает, тесты проходят

---

### Фаза 2: Создание AgentSessionManager

**Цель**: Управление персистентными сессиями агентов (session_context) на уровне workers-spawner.

#### Шаг 2.1: Создать AgentSessionManager

**Файл**: `services/workers-spawner/src/workers_spawner/session_manager.py` (новый)

**Содержание**:
```python
"""Agent session state management."""

import redis.asyncio as redis
import json
import structlog

logger = structlog.get_logger()

# Redis key: agent_session:{agent_id}
# TTL: совпадает с TTL контейнера (2 часа по умолчанию)

class AgentSessionManager:
    """Manages agent session context in Redis.

    Session context - агент-специфичное состояние (например, session_id для Claude).
    Хранится отдельно от метаданных контейнера.
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def get_session_context(self, agent_id: str) -> dict | None:
        """Get session context for agent."""
        key = f"agent_session:{agent_id}"
        data = await self.redis.get(key)

        if not data:
            return None

        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.warning("invalid_session_context", agent_id=agent_id)
            return None

    async def save_session_context(
        self,
        agent_id: str,
        context: dict,
        ttl_seconds: int = 7200,  # 2 hours default
    ) -> None:
        """Save session context for agent."""
        key = f"agent_session:{agent_id}"
        data = json.dumps(context)

        await self.redis.set(key, data, ex=ttl_seconds)

        logger.debug(
            "session_context_saved",
            agent_id=agent_id,
            context_keys=list(context.keys()),
        )

    async def delete_session_context(self, agent_id: str) -> None:
        """Delete session context (on container deletion)."""
        key = f"agent_session:{agent_id}"
        await self.redis.delete(key)
```

**Тесты**: `services/workers-spawner/tests/unit/test_session_manager.py`
- Mock Redis
- Тест save/get/delete
- Тест TTL
- Тест невалидного JSON

**Критерий завершения**: Session manager работает изолированно

---

#### Шаг 2.2: Интегрировать SessionManager в ContainerService

**Файл**: `services/workers-spawner/src/workers_spawner/container_service.py`

**Изменения**:
```python
class ContainerService:
    def __init__(self):
        self.settings = get_settings()
        self._containers: dict[str, dict] = {}

        # Добавляем session manager
        import redis.asyncio as redis
        self.redis = redis.from_url(self.settings.redis_url, decode_responses=True)
        from workers_spawner.session_manager import AgentSessionManager
        self.session_manager = AgentSessionManager(self.redis)

    # ... существующие методы ...

    async def delete(self, agent_id: str) -> bool:
        """Stop and remove container."""
        # ... существующий код ...

        # Добавляем очистку сессии
        await self.session_manager.delete_session_context(agent_id)

        # ... остальной код ...
```

**Тесты**: Обновить интеграционные тесты для ContainerService
- Проверить, что при удалении контейнера удаляется сессия

**Критерий завершения**: Session manager интегрирован, тесты проходят

---

### Фаза 3: Добавление send_message API в workers-spawner

**Цель**: Создать высокоуровневый API endpoint для отправки сообщений агентам.

#### Шаг 3.1: Добавить обработчик send_message

**Файл**: `services/workers-spawner/src/workers_spawner/redis_handlers.py`

**Изменения**:
```python
class CommandHandler:
    def __init__(self, ...):
        # ... существующий код ...

        self._handlers = {
            "create": self._handle_create,
            "send_command": self._handle_send_command,
            "send_message": self._handle_send_message,  # ← НОВЫЙ
            "send_file": self._handle_send_file,
            "status": self._handle_status,
            "logs": self._handle_logs,
            "delete": self._handle_delete,
        }

    # ... существующие методы ...

    async def _handle_send_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle send_message command.

        Expected fields:
        - agent_id: str
        - message: str (user message text)

        Returns:
        - response: str (agent response text)
        - metadata: dict (optional agent metadata)
        """
        agent_id = message.get("agent_id")
        user_message = message.get("message")

        if not agent_id or not user_message:
            raise ValueError("Missing 'agent_id' or 'message' field")

        # Получить метаданные контейнера
        metadata = self.containers._containers.get(agent_id)
        if not metadata:
            raise ValueError(f"Agent {agent_id} not found")

        config: WorkerConfig = metadata["config"]

        # Получить фабрику для этого типа агента
        from workers_spawner.factories.registry import get_agent_factory
        factory = get_agent_factory(config.agent)

        # Получить session context из Redis
        session_context = await self.containers.session_manager.get_session_context(agent_id)

        # Отправить сообщение через фабрику
        result = await factory.send_message(
            agent_id=agent_id,
            message=user_message,
            session_context=session_context,
        )

        # Сохранить обновлённый session context
        new_context = result.get("session_context")
        if new_context:
            ttl = config.ttl_hours * 3600
            await self.containers.session_manager.save_session_context(
                agent_id, new_context, ttl_seconds=ttl
            )

        # Publish event
        await self.events.publish_message(
            agent_id=agent_id,
            role="assistant",
            content=result["response"],
        )

        return {
            "response": result["response"],
            "metadata": result.get("metadata", {}),
        }
```

**Тесты**: `services/workers-spawner/tests/integration/test_send_message_handler.py`
- Mock Redis
- Mock фабрику
- Тест успешной отправки
- Тест с session context
- Тест без session context
- Тест с несуществующим agent_id

**Критерий завершения**: Handler работает, интеграционные тесты проходят

---

#### Шаг 3.2: Добавить метод get_agent_factory в registry

**Файл**: `services/workers-spawner/src/workers_spawner/factories/registry.py`

**Изменения**:
```python
# ... существующий код ...

def get_agent_factory(agent_type: AgentType) -> AgentFactory:
    """Get factory instance for agent type.

    Args:
        agent_type: Type of agent

    Returns:
        Factory instance

    Raises:
        ValueError: If agent type not registered
    """
    factory_class = AGENT_REGISTRY.get(agent_type)
    if not factory_class:
        raise ValueError(
            f"No factory registered for agent type: {agent_type}. "
            f"Available types: {list(AGENT_REGISTRY.keys())}"
        )

    return factory_class()
```

**Тесты**: Добавить в существующие тесты registry
- Тест get_agent_factory для известных типов
- Тест exception для неизвестного типа

**Критерий завершения**: Registry работает, тесты проходят

---

#### Шаг 3.3: Добавить publish_message в EventPublisher

**Файл**: `services/workers-spawner/src/workers_spawner/events.py`

**Изменения**:
```python
class EventPublisher:
    # ... существующий код ...

    async def publish_message(
        self,
        agent_id: str,
        role: str,  # "user" | "assistant"
        content: str,
    ) -> None:
        """Publish agent message event.

        Useful for logging, analytics, debugging.
        """
        await self._publish({
            "event": "agent.message",
            "agent_id": agent_id,
            "role": role,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
        })
```

**Тесты**: Добавить тест в существующие тесты events

**Критерий завершения**: Event publishing работает

---

### Фаза 4: Обновление Telegram Bot Client

**Цель**: Добавить метод `send_message()` в клиент workers-spawner (бот пока не использует).

#### Шаг 4.1: Расширить WorkersSpawnerClient

**Файл**: `services/telegram_bot/src/clients/workers_spawner.py`

**Изменения**:
```python
class WorkersSpawnerClient:
    # ... существующие методы ...

    async def send_message(
        self,
        agent_id: str,
        message: str,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """Send text message to agent.

        High-level API для общения с агентом.
        Абстрагирует детали CLI, session management, etc.

        Args:
            agent_id: Agent container ID
            message: User message text
            timeout: Request timeout in seconds

        Returns:
            {
                "response": str,  # Agent's response
                "metadata": dict,  # Optional metadata
            }
        """
        response = await self._request(
            "send_message",
            {"agent_id": agent_id, "message": message},
            timeout=float(timeout + 5),
        )

        if not response.get("success", False):
            raise RuntimeError(f"send_message failed: {response.get('error')}")

        return {
            "response": response["response"],
            "metadata": response.get("metadata", {}),
        }
```

**Тесты**: `services/telegram_bot/tests/unit/test_workers_spawner_client.py`
- Mock Redis request/response
- Тест успешной отправки
- Тест ошибки

**Критерий завершения**: Клиент обновлён, тесты проходят, но ПОКА НЕ ИСПОЛЬЗУЕТСЯ в боте

---

### Фаза 5: Миграция Telegram Bot (постепенная)

> **ПРИМЕЧАНИЕ**: Фаза реализована упрощенно без постепенного rollout и feature flags.
> Легаси код полностью удален и переписан, т.к. система в разработке.
> См. коммит 179f8ee для финальной реализации.

**Цель**: Перевести бот с `send_command` на `send_message`, убрать Claude-специфичную логику.

#### Шаг 5.1: Создать новый метод send_message_v2 в AgentManager

**Файл**: `services/telegram_bot/src/agent_manager.py`

**Изменения**:
```python
class AgentManager:
    # ... существующие методы ...

    async def send_message_v2(self, user_id: int, message: str) -> str:
        """Send message using new high-level API.

        Временный метод для A/B тестирования и постепенной миграции.
        """
        agent_id = await self.get_or_create_agent(user_id)

        logger.info(
            "sending_message_v2",
            user_id=user_id,
            agent_id=agent_id,
        )

        try:
            result = await workers_spawner.send_message(agent_id, message, timeout=120)
            return result["response"]
        except Exception as e:
            logger.error("send_message_v2_failed", error=str(e), user_id=user_id)
            raise
```

**Критерий завершения**: Новый метод существует, но не используется

---

#### Шаг 5.2: Feature Flag для постепенного rollout

**Файл**: `services/telegram_bot/src/config.py`

**Изменения**:
```python
class Settings(BaseSettings):
    # ... существующие поля ...

    use_new_agent_api: bool = Field(
        default=False,
        description="Use new send_message API instead of legacy send_command"
    )

    new_api_user_whitelist: str = Field(
        default="",
        description="Comma-separated Telegram user IDs for new API testing"
    )

    def is_user_in_whitelist(self, user_id: int) -> bool:
        """Check if user is whitelisted for new API."""
        if not self.new_api_user_whitelist:
            return False
        whitelist = [int(uid.strip()) for uid in self.new_api_user_whitelist.split(",")]
        return user_id in whitelist
```

**Файл**: `.env.example`
```bash
# Agent API migration
USE_NEW_AGENT_API=false
NEW_API_USER_WHITELIST=  # Comma-separated user IDs for testing
```

**Критерий завершения**: Feature flag настроен

---

#### Шаг 5.3: Обновить handle_message для использования feature flag

**Файл**: `services/telegram_bot/src/main.py`

**Изменения**:
```python
async def handle_message(update: Update, context) -> None:
    """Handle incoming messages - send to AgentManager."""
    # ... существующий код до send_message ...

    try:
        # ... логирование в RAG ...

        # Feature flag: выбираем API
        settings = get_settings()
        use_v2 = settings.use_new_agent_api or settings.is_user_in_whitelist(user_id)

        if use_v2:
            logger.info("using_new_agent_api", user_id=user_id)
            response_text = await agent_manager.send_message_v2(user_id, text)
        else:
            logger.info("using_legacy_agent_api", user_id=user_id)
            response_text = await agent_manager.send_message(user_id, text)

        # ... отправка ответа пользователю ...
```

**Критерий завершения**: Feature flag работает, можно переключать через .env

---

#### Шаг 5.4: Тестирование на whitelist пользователях

**План тестирования**:
1. Добавить свой Telegram ID в `NEW_API_USER_WHITELIST`
2. Перезапустить бот
3. Отправить несколько тестовых сообщений
4. Проверить:
   - Ответы корректные
   - Session сохраняется между сообщениями
   - Логи показывают `using_new_agent_api`
   - Нет ошибок в workers-spawner

**Rollback plan**: Убрать ID из whitelist, перезапустить бот

**Критерий завершения**:
- Новый API работает для whitelist пользователей
- Старый API работает для остальных
- Нет критических багов

---

#### Шаг 5.5: Постепенный rollout

**План**:
1. **День 1-2**: Whitelist (1-2 тестовых юзера) → мониторинг
2. **День 3-5**: 10% пользователей (`USE_NEW_AGENT_API=false`, расширенный whitelist)
3. **День 6-7**: 50% пользователей (случайный выбор в коде)
4. **День 8**: 100% пользователей (`USE_NEW_AGENT_API=true`)
5. **День 9-14**: Мониторинг стабильности
6. **День 15**: Удалить старый код

**Метрики для мониторинга**:
- Время ответа (latency)
- Частота ошибок
- Количество успешных/неуспешных сообщений
- Логи workers-spawner

**Критерий завершения**: 100% пользователей на новом API, стабильная работа неделю

---

#### Шаг 5.6: Удаление легаси кода

**Файл**: `services/telegram_bot/src/agent_manager.py`

**Удалить**:
- Метод `send_message()` (старый)
- Redis ключи для session_id в боте (`telegram:user_session_id:{user_id}`)
- Импорты для JSON парсинга (если не используются)

**Переименовать**:
- `send_message_v2()` → `send_message()`

**Файл**: `services/telegram_bot/src/config.py`

**Удалить**:
- `use_new_agent_api`
- `new_api_user_whitelist`
- Метод `is_user_in_whitelist()`

**Файл**: `services/telegram_bot/src/main.py`

**Упростить**:
```python
async def handle_message(update: Update, context) -> None:
    # ... существующий код ...

    # Упрощённый вызов (без feature flag)
    response_text = await agent_manager.send_message(user_id, text)

    # ... остальной код ...
```

**Миграция данных Redis**:
```bash
# Скрипт для очистки старых session_id ключей
# services/telegram_bot/scripts/cleanup_legacy_sessions.py

import redis
import asyncio

async def cleanup_legacy_sessions():
    r = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

    pattern = "telegram:user_session_id:*"
    keys = []
    async for key in r.scan_iter(match=pattern):
        keys.append(key)

    if keys:
        deleted = await r.delete(*keys)
        print(f"Deleted {deleted} legacy session keys")
    else:
        print("No legacy keys found")

    await r.aclose()

if __name__ == "__main__":
    asyncio.run(cleanup_legacy_sessions())
```

**Запуск очистки**:
```bash
make shell
cd /app/services/telegram_bot
python scripts/cleanup_legacy_sessions.py
```

**Тесты**: Удалить тесты для старого API, обновить документацию

**Критерий завершения**: Легаси код удалён, система работает на чистом коде

---

### Фаза 6: Улучшения и оптимизации (опционально)

#### Шаг 6.1: Persistent процесс агента (вместо fork на каждое сообщение)

**Проблема**: Сейчас каждый `send_message` = новый процесс `claude`.

**Решение**: Запустить `claude` в REPL/daemon режиме при создании контейнера.

**Исследование**:
- Есть ли у Claude CLI REPL режим?
- Можно ли использовать WebSocket к Claude API напрямую?
- Или stdio-based IPC с persistent процессом?

**Файл**: `services/workers-spawner/src/workers_spawner/factories/agents/claude_code.py`

**Концепция**:
```python
class ClaudeCodeAgent(AgentFactory):

    async def start_persistent_session(self, agent_id: str) -> None:
        """Start Claude in persistent REPL mode (if available)."""
        # TODO: Research Claude CLI REPL mode
        pass

    async def send_message(self, agent_id: str, message: str, ...) -> dict:
        """Send to persistent process instead of forking."""
        # TODO: Implement IPC with persistent process
        pass
```

**Критерий завершения**: Исследование завершено, решение задокументировано

---

#### Шаг 6.2: Streaming responses

**Проблема**: Пользователь ждёт всего ответа целиком.

**Решение**: Streaming API для постепенной отправки ответа.

**Новый метод**:
```python
class AgentFactory(ABC):
    async def stream_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> AsyncIterator[str]:
        """Stream agent response token by token."""
        yield "Not implemented"
```

**Интеграция в Telegram**:
- Отправлять частичные ответы (edit_message_text каждые N токенов)
- Или показывать индикатор "Claude печатает..."

**Критерий завершения**: Streaming работает (если Claude CLI поддерживает)

---

#### Шаг 6.3: Metrics и observability

**Добавить метрики**:
- `agent_message_duration_seconds` (histogram)
- `agent_message_errors_total` (counter)
- `agent_session_active` (gauge)

**Файл**: `services/workers-spawner/src/workers_spawner/metrics.py` (новый)

**Интеграция**: Prometheus + Grafana dashboard

**Критерий завершения**: Metrics собираются, dashboard создан

---

## Чеклист перед началом работы

- [ ] Review этого плана с командой
- [ ] Согласовать приоритеты фаз (можно ли пропустить 6?)
- [ ] Настроить CI для автоматического запуска тестов
- [ ] Создать feature branch `refactor/agent-abstraction`
- [ ] Настроить staging окружение для тестирования

---

## Риски и митигация

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Claude CLI меняет формат вывода | Средняя | Высокое | Версионирование CLI, fallback парсинг |
| Регрессия в production | Низкая | Критическое | Feature flag, постепенный rollout |
| Производительность хуже | Низкая | Среднее | Benchmarking до/после, мониторинг |
| Несовместимость с Factory Droid | Средняя | Среднее | Сначала реализовать для Claude, потом адаптировать |
| Потеря сессий при миграции | Низкая | Низкое | Сессии короткоживущие, можно пересоздать |

---

## Оценка трудозатрат

| Фаза | Задач | Оценка (дни) | Зависимости |
|------|-------|--------------|-------------|
| 0. Подготовка | 1 | 0.5 | - |
| 1. AgentFactory расширение | 3 | 2-3 | Фаза 0 |
| 2. SessionManager | 2 | 1-2 | Фаза 1 |
| 3. workers-spawner API | 3 | 2-3 | Фаза 1, 2 |
| 4. Bot client | 1 | 1 | Фаза 3 |
| 5. Миграция бота | 6 | 5-7 | Фаза 4 |
| 6. Оптимизации | 3 | 3-5 (опционально) | Фаза 5 |
| **Итого (без фазы 6)** | **16** | **11-16 дней** | - |
| **Итого (с фазой 6)** | **19** | **14-21 день** | - |

**Рекомендация**: Выполнить фазы 0-5 (MVP), фазу 6 отложить на потом.

---

## Критерии успеха проекта

1. ✅ Telegram бот не содержит упоминаний "claude", "session_id", JSON парсинга
2. ✅ `agent_manager.py` использует только `send_message(text) -> text`
3. ✅ Можно добавить нового агента без изменений в боте
4. ✅ Все тесты проходят (coverage >= 80%)
5. ✅ Нет регрессии в production (мониторинг 2 недели)
6. ✅ Документация обновлена (README, ARCHITECTURE.md)

---

## Следующие шаги после завершения

1. Реализовать `FactoryDroidAgent.send_message()`
2. Добавить других агентов (Gemini CLI, Codex, etc.)
3. Рефакторинг инъекции зависимостей (ContainerService → фабрики)
4. Переиспользовать абстракцию для LangGraph нод (если нужны CLI агенты внутри графа)
5. Persistent sessions (фаза 6.1)
6. Streaming (фаза 6.2)

---

## Вопросы для обсуждения

1. Нужна ли фаза 6 (оптимизации) в MVP или можно потом?
2. Какой процент пользователей для первого rollout? (я предложил 10%)
3. Есть ли требования к обратной совместимости сессий?
4. Нужна ли миграция существующих session_id из бота в workers-spawner?
   - Скорее всего нет, т.к. сессии короткоживущие (TTL 2 часа)
5. Какие агенты планируются после Claude? (Factory Droid точно?)

---

## Приложение: Примеры использования

### До (текущий код в боте)

```python
# agent_manager.py
session_id = await self.redis.get(f"telegram:user_session_id:{user_id}")
safe_message = message.replace("'", "'\\''")
cmd = f"claude -p '{safe_message}' --resume {session_id} --output-format json"
result = await workers_spawner.send_command(agent_id, cmd, timeout=120)
data = json.loads(result["output"])
response = data["result"]
await self.redis.set(f"telegram:user_session_id:{user_id}", data["session_id"])
return response
```

### После (целевой код в боте)

```python
# agent_manager.py
agent_id = await self.get_or_create_agent(user_id)
result = await workers_spawner.send_message(agent_id, message)
return result["response"]
```

### Добавление нового агента (после рефакторинга)

```python
# services/workers-spawner/src/workers_spawner/factories/agents/gemini_cli.py

from workers_spawner.models import AgentType
from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent

@register_agent(AgentType.GEMINI_CLI)
class GeminiCLIAgent(AgentFactory):
    def get_install_commands(self) -> list[str]:
        return ["pip install google-generativeai"]

    def get_agent_command(self) -> str:
        return "gemini-cli"  # Hypothetical

    def get_required_env_vars(self) -> list[str]:
        return ["GOOGLE_API_KEY"]

    def generate_instructions(self, allowed_tools) -> dict[str, str]:
        content = get_instructions_content(allowed_tools)
        return {"/workspace/GEMINI.md": content}

    async def send_message(self, agent_id: str, message: str, session_context=None):
        # Gemini-specific implementation
        ...
```

**Бот не меняется!** Только конфиг:
```python
# telegram_bot создаёт агента с
config = {
    "agent": "gemini-cli",  # ← поменяли тип
    ...
}
```

---

**Документ обновлён**: 2026-01-04
**Автор**: Claude Sonnet 4.5
**Статус**: Draft, ожидает review
