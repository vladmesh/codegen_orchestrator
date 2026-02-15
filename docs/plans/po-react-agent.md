# Plan: PO как LangGraph ReactAgent (без контейнера)

> **Дата**: 2026-02-15
> **Статус**: Phase 2.4 Complete, Phase 2.5 next
> **Контекст**: MVP работает, E2E проходят. PO — самый перегруженный компонент: Docker-контейнер, subprocess Claude CLI, orchestrator-cli как прокси к API/Redis. Каждая будущая фича упирается в эту архитектуру.

---

## Проблема текущей архитектуры

### Путь сообщения: 7 хопов

```
User → Telegram Bot → Redis (worker:{id}:input)
  → worker-wrapper (в контейнере) → subprocess(claude -p "..." --output-format json)
    → Claude Code CLI → orchestrator-cli → HTTP/Redis (API, queues)
      → ответ через Redis (cli-agent:user-messages ИЛИ worker:{id}:output)
        → Telegram Bot → User
```

Каждый хоп — точка отказа, латентность, сложность дебага. Для координатора, который **не работает с файлами**, это неоправданный overhead.

### Что PO реально делает

Анализ текущих инструкций (`shared/prompts/po_worker/INSTRUCTIONS.md`) и CLI-команд:

| Действие | CLI-команда | Реальная операция |
|----------|------------|-------------------|
| Создать проект | `orchestrator project create` | `POST /api/projects/` |
| Установить секрет | `orchestrator project set-secret` | `GET + PATCH /api/projects/{id}` |
| Список проектов | `orchestrator project list` | `GET /api/projects/` |
| Триггер engineering | `orchestrator engineering trigger` | `POST /api/tasks/` + `XADD engineering:queue` |
| Статус задачи | `orchestrator engineering status` | `GET /api/tasks/{id}` |
| Триггер деплоя | `orchestrator deploy trigger` | `POST /api/tasks/` + `XADD deploy:queue` |
| Ответить пользователю | `orchestrator respond` | `XADD cli-agent:user-messages` |

**Всё это — HTTP-запросы к API и XADD в Redis.** Контейнер, Docker, файловая система, git — ни для чего из этого не нужны.

### Стоимость контейнерного подхода

**Ресурсы**: Каждый PO-контейнер ~500-800MB RAM (Python + Claude Code CLI + Git + зависимости). При 20 пользователях — 10-16GB только на PO. Worker-wrapper + Claude subprocess внутри контейнера — ещё CPU.

**Латентность**: Первое сообщение пользователя = создание контейнера (build image, если нет кеша — до 2 мин). Каждое последующее = subprocess(claude) startup. `WORKER_CREATION_TIMEOUT = 120s` в коде.

**Дебаг**: PO — чёрная коробка. Видим только итоговый JSON из Claude CLI. Промежуточные рассуждения, tool calls, ошибки — всё скрыто внутри subprocess. `structlog` снаружи логирует только "message_sent_to_worker" / "worker_response_received".

**Хрупкость**: Цепочка Docker → worker-wrapper → Claude CLI → orchestrator-cli. Любой баг в orchestrator-cli (парсинг аргументов, permission check, async runtime) = PO сломан. Claude Code CLI может обновиться и изменить output format.

### Масштабирование

```
Текущая модель:
  20 users × 1 PO container = 20 containers × ~700MB = ~14GB RAM
  100 users = ~70GB RAM (только PO, без developer-воркеров)

API-модель:
  20 users = 0 containers, ~0 RAM overhead
  100 users = 0 containers, ~0 RAM overhead
  (stateless API calls, memory только на время обработки запроса)
```

---

## Целевая архитектура

### Упрощённый путь сообщения

```
Было (7 хопов):
  User → Telegram → Redis (worker:input) → worker-manager → Docker container
    → worker-wrapper → subprocess(claude) → orchestrator-cli → HTTP/Redis
    → Redis (worker:output / cli-agent:user-messages) → Telegram → User

Станет (4 хопа, тот же Redis-паттерн, без контейнера):
  User → Telegram → Redis (po:input)
    → PO consumer (в langgraph контейнере) → ainvoke(PO graph) → tools (API/Redis)
    → Redis (po:response:{request_id}) → Telegram → User
```

### Принцип: PO не живёт — он вызывается

PO не держит открытое соединение с LLM. Каждый вызов — короткий (секунды), stateless между вызовами. State хранится в checkpointer. Три типа триггеров:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        ТРИГГЕРЫ PO                                      │
├──────────────────┬──────────────────────────────────────────────────────┤
│  User message    │  Telegram → po:input → ainvoke(PO)                   │
│                  │  "создай бота", "как дела?"                          │
│                  │                                                      │
│  System event    │  Engineering/Deploy → po:input → ainvoke(PO)         │
│                  │  task_completed, task_failed, scaffolding_done        │
│                  │                                                      │
│  Timer           │  Scheduler → po:input → ainvoke(PO)                  │
│                  │  "проверь статус через 10 минут"                     │
└──────────────────┴──────────────────────────────────────────────────────┘
```

PO — **единственная точка коммуникации с пользователем**:
1. PO **решает** нужно ли пересылать событие пользователю (фильтрация по предпочтениям)
2. PO **переводит** технические ошибки в человеческий язык
3. PO **может действовать** — поймать ошибку и перезапустить задачу молча

### Пример полного workflow

```
t=0s    User: "Создай бота для пиццерии, вот токен: 123:ABC"
        → ainvoke(PO)
        → PO: create_project() → set_secret() → trigger_engineering()
        → PO → User: "Начал разработку! Напишу когда будет готово."
        → done (3 сек)

t=30s   Event: scaffolding_completed
        → ainvoke(PO, system_message="Scaffolding завершён для проекта X")
        → PO решает: пользователю неинтересны технические детали → молчит
        → done (1 сек)

t=5m    Event: developer_started
        → ainvoke(PO, system_message="Developer начал работу")
        → PO → User: "Агент начал писать код"
        → done (1 сек)

t=15m   Event: engineering_completed {status: "success", url: "..."}
        → ainvoke(PO, system_message="Engineering завершён, URL: ...")
        → PO → User: "Бот готов! Вот ссылка: https://..."
        → done (2 сек)
```

Каждый вызов — секунды. Между вызовами PO не существует. Нет idle-контейнера, нет открытого LLM-соединения.

### Что убираем из PO flow

| Компонент | Роль сейчас | Что с ним |
|-----------|------------|-----------|
| worker-manager (для PO) | Создаёт PO-контейнер | **Не нужен для PO.** Остаётся для Developer/Tester |
| worker-wrapper | Запускает Claude CLI в контейнере | **Не нужен для PO.** Остаётся для Developer/Tester |
| orchestrator-cli (PO tools) | CLI-прокси к API/Redis | **Заменяется** Python-функциями (tools). CLI остаётся для Developer |
| Docker-контейнер PO | Изоляция | **Не нужен.** PO не работает с FS |
| `session:po:{user_id}` → worker_id | Маппинг user → container | **Заменяется** на `thread_id` в LangGraph checkpointer |
| `worker:{id}:input/output` streams | Коммуникация с контейнером | **Заменяются** на `po:input` и `po:response:{request_id}` |
| `cli-agent:user-messages` stream | PO → пользователь | **Не нужен.** Ответ через `po:response:{request_id}` |

### Что остаётся без изменений

| Компонент | Почему |
|-----------|--------|
| `engineering:queue` | Engineering subgraph слушает эту очередь. PO tools пишут туда напрямую |
| `deploy:queue` | Deploy subgraph слушает эту очередь |
| API endpoints (`/api/projects/`, `/api/tasks/`) | PO tools вызывают их напрямую через httpx |
| Developer/Tester containers | Остаются в Docker (работают с FS) |
| worker-manager (для Developer) | Остаётся |

---

## Фаза 1: PO Graph + Tools + Consumer

### 1.1 PO граф

```python
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

model = ChatOpenAI(
    model=PO_LLM_MODEL,       # "anthropic/claude-sonnet-4-5"
    base_url=PO_LLM_BASE_URL,  # "https://openrouter.ai/api/v1"
    api_key=PO_LLM_API_KEY,
)

po_graph = create_react_agent(
    model=model,
    tools=tools,
    state_modifier=trim_messages,
    checkpointer=checkpointer,
)
```

Граф живёт в `services/langgraph/src/po/graph.py`. Consumer подключается в `src/worker/main.py`:

```python
async def run_worker():
    await asyncio.gather(
        listen_provisioner_triggers(),   # существующий pub/sub listener
        listen_worker_events(),          # существующий pub/sub listener
        run_po_consumer(),               # новое: stream consumer для PO
    )
```

Основной `langgraph` контейнер сейчас недогружен (два лёгких pub/sub listener'а). Если нагрузка вырастет — выносим в отдельный контейнер с отдельным CMD, код менять не нужно.

### 1.2 PO tools

PO tools пишутся заново как async-функции. orchestrator-cli не трогаем — остаётся для Developer/Tester. Причины:

1. **Паттерны разные.** CLI создаёт httpx/redis клиент на каждый вызов. PO хочет shared client — за один invoke может вызвать 3-4 tools подряд.
2. **Логики мало.** 7 функций × ~15 строк = ~100 строк. Не стоит coupling ради 100 строк.

```python
from langchain_core.tools import tool

# Shared client — создаётся один раз при старте consumer'а
api_client: httpx.AsyncClient  # base_url=API_BASE_URL
redis_client: Redis             # from REDIS_URL

@tool
async def create_project(name: str, modules: str = "backend", description: str = "") -> str:
    """Create a new project with specified modules.

    Args:
        name: Project name
        modules: Comma-separated modules (backend, tg_bot, notifications, frontend)
        description: What the project should do
    """
    modules_list = [m.strip() for m in modules.split(",")]
    response = await api_client.post("/api/projects/", json={
        "id": str(uuid.uuid4()),
        "name": name,
        "status": "draft",
        "config": {"modules": modules_list, "description": description},
    })
    response.raise_for_status()
    project = response.json()
    return f"Project created. ID: {project['id']}, Name: {project['name']}"
```

Полный список tools:

| Tool | Операция | Источник данных |
|------|----------|----------------|
| `create_project` | `POST /api/projects/` | API |
| `list_projects` | `GET /api/projects/` | API |
| `get_project` | `GET /api/projects/{id}` | API |
| `set_project_secret` | `PATCH /api/projects/{id}` | API |
| `trigger_engineering` | `POST /api/tasks/` + `XADD engineering:queue` | API + Redis |
| `trigger_deploy` | `POST /api/tasks/` + `XADD deploy:queue` | API + Redis |
| `get_task_status` | `GET /api/tasks/{id}` | API |
| `set_reminder` | `ZADD po:reminders` — delayed message в `po:input` | Redis |

### 1.3 Consumer: единый стрим + concurrency

**Один общий стрим** `po:input` с полем `user_id` вместо per-user стримов `po:input:{user_id}`. Преимущества:
- Один consumer group, один `XREADGROUP` — нет `discover_active_streams()` проблемы
- Проще мониторинг (один стрим, один lag metric)
- Изоляция не нужна — PO consumer один процесс

**Concurrent processing.** Разные пользователи обрабатываются параллельно. Сообщения одного пользователя — последовательно (через per-user lock):

```python
# services/langgraph/src/po/consumer.py

MAX_CONCURRENT = 10
user_locks: dict[str, asyncio.Lock] = {}  # per-user serialization

async def run_po_consumer():
    """Main loop: read po:input, invoke PO graph, write po:response:*."""
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    # Ensure consumer group exists
    await redis.xgroup_create("po:input", "po-consumer", mkstream=True)

    while True:
        entries = await redis.xreadgroup(
            "po-consumer", "worker-0", {"po:input": ">"}, count=10, block=5000
        )

        for stream_name, messages in entries:
            for msg_id, data in messages:
                asyncio.create_task(
                    _process_message(sem, msg_id, data)
                )

async def _process_message(sem: asyncio.Semaphore, msg_id: str, data: dict):
    user_id = data["user_id"]
    lock = user_locks.setdefault(user_id, asyncio.Lock())

    async with sem:        # global concurrency limit
        async with lock:   # per-user serialization
            try:
                await _handle_message(user_id, data)
            except Exception:
                logger.exception("po_invoke_failed", user_id=user_id, msg_id=msg_id)
                # Ответить пользователю об ошибке, если есть request_id
                if data.get("request_id"):
                    await redis.xadd(f"po:response:{data['request_id']}", {
                        "text": "Произошла ошибка, попробуйте ещё раз.",
                        "user_id": user_id,
                        "error": "true",
                    })
            finally:
                await redis.xack("po:input", "po-consumer", msg_id)

async def _handle_message(user_id: str, data: dict):
    # Форматируем с timestamp
    formatted = f"[{data['timestamp']} UTC] {data['text']}"

    if data["type"] == "user_message":
        msg = HumanMessage(content=formatted)
    else:  # system_event, reminder
        msg = SystemMessage(content=formatted)

    result = await po_graph.ainvoke(
        {"messages": [msg]},
        config={"configurable": {"thread_id": f"po-user-{user_id}"}},
    )

    response_text = result["messages"][-1].content

    if data.get("request_id"):
        await redis.xadd(f"po:response:{data['request_id']}", {
            "text": response_text,
            "user_id": user_id,
        })
    # system events без request_id: PO может промолчать или
    # инициировать proactive message через po:proactive:{user_id}
```

**Error handling:**
- `try/except` вокруг `ainvoke` — при ошибке LLM API пользователь получает fallback-сообщение, consumer продолжает работу
- `xack` в `finally` — сообщение не застрянет в pending при ошибке
- Per-user lock — ошибка одного пользователя не блокирует других

**Graceful shutdown.** При SIGTERM: перестаём читать новые сообщения, ждём завершения текущих `_process_message` tasks (с timeout), затем останавливаемся. Последние ACK'd сообщения = точка возобновления.

### 1.4 Conversation history

**PostgreSQL checkpointer** хранит полную историю. **Message trimming** контролирует контекстное окно:

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver(conn_string=DATABASE_URL)

# Каждый пользователь = отдельный thread (навсегда, без сессий)
config = {"configurable": {"thread_id": f"po-user-{user_id}"}}
```

Пользователь не управляет контекстом. Один thread навсегда + auto-trimming:

```python
def trim_messages(messages, max_tokens=50_000):
    """Keep system prompt + last N messages that fit in token budget."""
    system = [m for m in messages if m.type == "system"]
    rest = [m for m in messages if m.type != "system"]

    kept = []
    tokens = 0
    for msg in reversed(rest):
        msg_tokens = estimate_tokens(msg)
        if tokens + msg_tokens > max_tokens:
            break
        kept.insert(0, msg)
        tokens += msg_tokens

    return system + kept

po_graph = create_react_agent(
    model=model,
    tools=tools,
    state_modifier=trim_messages,
    checkpointer=checkpointer,
)
```

```
User 123:
  thread_id = "po-user-123" (создаётся при первом сообщении, живёт вечно)

  Контекст окно:
  ┌─────────────────────────────────────────────────────┐
  │ System prompt (всегда)                               │
  │                                                      │
  │ ... старые сообщения отрезаны ...                    │
  │                                                      │
  │ [2026-02-14 10:15 UTC] User: "как дела с ботом?"    │
  │ [2026-02-14 10:15 UTC] PO: "Бот работает, ..."     │
  │ [2026-02-15 14:30 UTC] User: "добавь /menu команду" │
  │ [2026-02-15 14:31 UTC] PO: "Запустил разработку..." │
  │ [2026-02-15 14:45 UTC] System: engineering completed │
  │ [2026-02-15 14:45 UTC] PO: "Готово! Проверь бота"   │
  └─────────────────────────────────────────────────────┘
```

### 1.5 LLM-провайдер: OpenRouter

**Для фазы экспериментов OpenRouter — оптимальный выбор.** Один ключ, один формат, смена модели = смена env var.

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model=PO_LLM_MODEL,
    base_url=PO_LLM_BASE_URL,
    api_key=PO_LLM_API_KEY,
)
```

Сравнение провайдеров:

| Провайдер | LangChain класс | Плюсы | Минусы |
|-----------|----------------|-------|--------|
| **OpenRouter** | `ChatOpenAI(base_url=...)` | Один ключ, все модели, быстрая смена | +50-200ms латентности, ещё одна точка отказа |
| Anthropic напрямую | `ChatAnthropic` | Минимальная латентность, prompt caching, extended thinking | Только Claude |
| OpenAI напрямую | `ChatOpenAI` | GPT-4o, o3 | Только OpenAI |

Когда переходить на прямой API: если 90%+ запросов идут на одну модель — переключить прямой `ChatAnthropic`/`ChatOpenAI` для основной, OpenRouter для fallback.

**Конфигурация** — env vars, без дефолтных значений:

```python
PO_LLM_MODEL = os.getenv("PO_LLM_MODEL")
PO_LLM_BASE_URL = os.getenv("PO_LLM_BASE_URL")
PO_LLM_API_KEY = os.getenv("PO_LLM_API_KEY")

if not all([PO_LLM_MODEL, PO_LLM_BASE_URL, PO_LLM_API_KEY]):
    raise RuntimeError("PO_LLM_MODEL, PO_LLM_BASE_URL, PO_LLM_API_KEY must be set")
```

### 1.6 System prompt

Адаптация `shared/prompts/po_worker/INSTRUCTIONS.md`:
- Убрать CLI-команды (`orchestrator project create` → описание tools)
- Добавить инструкции по обработке system events (когда молчать, когда сообщить, когда действовать)
- Описать поведение при ошибках (retry vs уведомить пользователя)
- Добавить инструкции по timestamps (PO видит временной контекст сообщений)

Файл: `services/langgraph/src/po/prompts.py`

### 1.7 Тесты

- **Unit-тесты tools**: Mock httpx/redis, проверить каждый tool
- **Graph integration test**: `FakeChatModel` из langchain, проверить что граф корректно вызывает tools и возвращает ответ
- **Consumer test**: Mock graph, проверить чтение из stream, routing по типу сообщения, error handling

### Файловая структура фазы 1

```
services/langgraph/src/po/
├── __init__.py
├── graph.py       — create_react_agent + config
├── tools.py       — 8 tools (create_project, trigger_engineering, ...)
├── prompts.py     — system prompt
└── consumer.py    — Redis stream consumer

services/langgraph/tests/unit/po/
├── test_tools.py
├── test_graph.py
└── test_consumer.py
```

---

## Фаза 1.5: Persistent Checkpointer (PostgreSQL)

> **Зависимость**: Phase 2 без персистентности бессмысленна — рестарт контейнера = потеря всех разговоров.

### Проблема

Phase 1 использует `MemorySaver` — in-memory checkpointer. Каждый рестарт langgraph контейнера стирает всю conversation history. Reminder'ы (`po:reminders` в Redis) переживают рестарт, но контекст разговора, к которому они относятся — нет.

### Решение: `AsyncPostgresSaver` + отдельная PostgreSQL schema

Checkpointer пишет в ту же PostgreSQL базу (`orchestrator`), но в отдельную schema `langgraph`. Alembic работает с `public` schema по умолчанию — **пересечений нет**, фильтры в `env.py` не нужны.

```
orchestrator (database)
├── public (schema)        ← Alembic, наши модели (projects, tasks, ...)
└── langgraph (schema)     ← checkpoint_*, управляется AsyncPostgresSaver.setup()
```

### Что хранит checkpointer

Каждый **super-step** графа (не только сообщения). Один запрос "создай бота" → 4-6 чекпоинтов:

```
User message → [checkpoint] → LLM думает → [checkpoint] → create_project() → [checkpoint]
→ set_secret() → [checkpoint] → trigger_engineering() → [checkpoint] → ответ → [checkpoint]
```

В каждом — полный `state["messages"]` на тот момент. Даёт возможность replay, но стоит места.

### Реализация

**1. SQL-миграция для schema** (ручная, не через Alembic):

```sql
-- scripts/init_langgraph_schema.sql
CREATE SCHEMA IF NOT EXISTS langgraph;
```

Вызывается при первом деплое или через `docker-entrypoint-initdb.d/`.

**2. `AsyncPostgresSaver` с search_path**:

```python
# services/langgraph/src/po/graph.py

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# Connection string с search_path на langgraph schema
# postgresql+asyncpg://user:pass@db:5432/orchestrator → для asyncpg (без +asyncpg)
CHECKPOINT_DB_URL = os.getenv("CHECKPOINT_DATABASE_URL")  # postgresql://user:pass@db:5432/orchestrator?options=-c%20search_path%3Dlanggraph

async def create_po_graph(...) -> CompiledStateGraph:
    checkpointer = AsyncPostgresSaver.from_conn_string(CHECKPOINT_DB_URL)
    await checkpointer.setup()  # CREATE TABLE IF NOT EXISTS в langgraph schema
    ...
```

**3. Env var в docker-compose.yml** для langgraph сервиса:

```yaml
CHECKPOINT_DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}?options=-c%20search_path%3Dlanggraph
```

Отдельный env var (не `DATABASE_URL`) чтобы явно разделить: API владеет `DATABASE_URL`, checkpointer — `CHECKPOINT_DATABASE_URL`.

**4. Зависимость**: `langgraph-checkpoint-postgres` в requirements.

**5. `create_po_graph` становится async** (из-за `checkpointer.setup()` и `from_conn_string`). Consumer вызывает его при старте.

### Файлы

```
scripts/init_langgraph_schema.sql          — CREATE SCHEMA IF NOT EXISTS langgraph
services/langgraph/src/po/graph.py         — MemorySaver → AsyncPostgresSaver
services/langgraph/src/config/settings.py  — + checkpoint_database_url
docker-compose.yml                         — + CHECKPOINT_DATABASE_URL для langgraph
services/langgraph/requirements.in         — + langgraph-checkpoint-postgres
```

### Тесты

- Unit-тесты графа продолжают использовать `MemorySaver` (не нужен PostgreSQL для unit-тестов)
- Integration test: запись/чтение checkpoint через `AsyncPostgresSaver` с реальным PostgreSQL

---

## Фаза 2: Telegram Integration + Event-Driven PO

### 2.1 Telegram bot: новый flow

```python
# services/telegram_bot/src/main.py (handle_message)

request_id = str(uuid.uuid4())
timestamp = datetime.now(UTC).isoformat()

# 1. Отправить typing indicator
await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

# 2. Publish в общий PO stream
await redis.xadd("po:input", {
    "type": "user_message",
    "text": text,
    "user_id": str(user_id),
    "request_id": request_id,
    "timestamp": timestamp,
})

# 3. Ждём ACK (consumer начал обработку) — обновляем typing
# 4. Ждём ответ
response = await redis.xread(
    {f"po:response:{request_id}": "$"},
    block=60000,  # 60s — PO может вызвать 3-4 tools по 5-8 сек каждый
)

# 5. Отправить пользователю
await context.bot.send_message(chat_id, response.text)
```

`POSessionManager` с логикой создания контейнеров **заменяется** минимальной обёрткой: publish в `po:input` + wait на `po:response:{request_id}`.

**Timestamps** в каждом сообщении дают PO временной контекст:
- Между "Хочу бота" и "Вот токен" прошло 10 минут → пользователь ходил в BotFather
- Между двумя сообщениями 2 секунды → пользователь дробит мысль

### 2.2 Typing indicator (**Done** — merged into Phase 2.1)

Реализовано в `_send_to_po_and_wait()`: typing task стартует сразу после `XADD`, без отдельного ACK-стрима. Проще и быстрее для пользователя — typing виден мгновенно. `po:ack:{request_id}` не понадобился.

### 2.3 System events: PO как единый хаб

**Scope**: PO — единственная точка коммуникации с пользователем для событий о проектах. Есть два исключения, которые обходят PO и остаются без изменений:

1. **Кнопки** (`handlers.py`) — прямые API calls (список проектов, детали, серверы). Экономят токены, результат возвращается как inline keyboard edit.
2. **Admin notifications** (`ProvisionerNotifier`) — ops/infra уведомления (provisioning results). Пишутся напрямую в Telegram админам. PO про них знать не должен.

```
Через PO (проектные события):
  Engineering worker → po:input {type: "system_event"} → PO → (решает) → Telegram
  Deploy worker → po:input {type: "system_event"} → PO → (решает) → Telegram
  Timer → po:input {type: "reminder"} → PO → (решает) → Telegram

Минуя PO (как есть):
  Кнопки → handlers.py → API → edit_message (inline keyboard)
  Provisioner → provisioner:results → ProvisionerNotifier → Telegram (админы)
```

#### Текущий callback_stream паттерн

Workers уже пишут события через `publish_callback_event()` в `po:events:{task_id}`. PO tools при триггере задают `callback_stream = f"po:events:{task_id}"`. Проблема: **никто не слушает** эти стримы. PO полагается на `set_reminder` + `get_task_status` (polling).

#### Решение: `callback_stream = "po:input"`

Вместо per-task стримов (`po:events:{task_id}`) отправляем события прямо в `po:input`. Формат адаптируется под consumer:

```python
# _events.py — расширенный publish_callback_event
async def publish_callback_event(
    redis, callback_stream, event_type, task_id, message,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
) -> None:
    if not callback_stream:
        return

    data = {
        "type": "system_event",
        "event": event_type,           # "progress", "completed", "failed"
        "task_id": task_id,
        "text": message,               # human-readable
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if user_id:
        data["user_id"] = user_id
    if project_id:
        data["project_id"] = project_id

    await redis.redis.xadd(callback_stream, data)
```

Workers уже имеют `user_id` и `project_id` в `job_data` — прокидываем в `publish_callback_event`. PO tools меняют `callback_stream`:

```python
# tools.py — trigger_engineering
callback_stream = PO_INPUT_QUEUE  # "po:input" вместо f"po:events:{task_id}"

queue_msg = {
    "task_id": task_id,
    "project_id": project_id,
    "user_id": user_id,          # прокидываем от PO
    "callback_stream": callback_stream,
    ...
}
```

**Преимущества:**
- Нет discovery проблемы (один стрим, один consumer)
- Workers не знают про PO — пишут в generic `callback_stream`
- Consumer уже умеет обрабатывать `type != "user_message"` как `SystemMessage`

#### user_id в tools

Сейчас PO tools не знают `user_id` — они вызываются из LLM как stateless функции. Варианты:

1. **RunnableConfig injection** — LangGraph пробрасывает `config` в tools через `@tool`. Consumer ставит `user_id` в config, tools читают:
   ```python
   @tool
   async def trigger_engineering(..., config: RunnableConfig) -> str:
       user_id = config["configurable"]["user_id"]
   ```

2. **Добавить `user_id` как аргумент tool** — PO должен передать user_id из контекста. Хрупко — LLM может забыть.

Вариант 1 (RunnableConfig) — надёжнее, user_id задаётся программно.

#### Consumer: обработка system events

Consumer (`_handle_message`) уже роутит по `type`: `user_message` → HumanMessage, всё остальное → SystemMessage. System events попадают в граф как SystemMessage, PO решает что делать.

**Но**: для system events нет `request_id` — Telegram bot не ждёт ответ. PO нужен способ отправить proactive message.

#### Proactive messages: `po:proactive` stream

PO consumer проверяет ответ графа на system event. Если PO решил сообщить пользователю:

```python
# consumer.py — _handle_message, после ainvoke для system events
if msg_type == "system_event" and response_text:
    # PO решил что-то сказать — proactive message
    await redis.xadd("po:proactive", {
        "text": response_text,
        "user_id": user_id,
    })
```

Telegram bot слушает `po:proactive` через consumer group:

```python
# telegram_bot — новый listener
async def _listen_proactive(redis, bot):
    """Listen for proactive PO messages to users."""
    await redis.xgroup_create("po:proactive", "tg-bot", mkstream=True)
    while True:
        entries = await redis.xreadgroup(
            "tg-bot", "bot-0", {"po:proactive": ">"}, count=10, block=5000
        )
        for _, messages in entries:
            for msg_id, data in messages:
                chat_id = int(data["user_id"])  # telegram user_id = chat_id for DMs
                await bot.send_message(chat_id, data["text"])
                await redis.xack("po:proactive", "tg-bot", msg_id)
```

Один стрим `po:proactive` с полем `user_id` (как `po:input`): один XREADGROUP, нет discovery проблемы.

#### Типы событий

| Event | Source | Когда |
|-------|--------|-------|
| `progress` | engineering/deploy worker | Начало работы, ожидание CI, этапы |
| `completed` | engineering/deploy worker | Задача успешно завершена |
| `failed` | engineering/deploy worker | Задача провалилась |

PO видит событие с контекстом (`task_id`, `project_id`, `message`) и решает:
- **Промолчать** — progress events (пользователю неинтересны промежуточные шаги)
- **Сообщить** — completed/failed (результат, который ждёт пользователь)
- **Действовать** — failed → перезапустить задачу (future)

#### Файлы

```
Изменения:
  services/langgraph/src/workers/_events.py           — +user_id, +project_id params
  services/langgraph/src/workers/engineering_worker.py — прокидывать user_id/project_id
  services/langgraph/src/workers/deploy_worker.py     — прокидывать user_id/project_id
  services/langgraph/src/po/tools.py                  — callback_stream = PO_INPUT_QUEUE, user_id из config
  services/langgraph/src/po/consumer.py               — proactive messages для system events
  services/telegram_bot/src/main.py                   — +_listen_proactive()
  shared/queues.py                                    — +PO_PROACTIVE_QUEUE

Новое:
  (нет новых файлов)

Без изменений:
  services/telegram_bot/src/handlers.py               — кнопки работают как есть
  services/telegram_bot/src/notifications.py           — admin notifications работают как есть
  services/telegram_bot/src/keyboards.py              — без изменений
```

#### Тесты

- **Unit**: mock graph, проверить что system event без request_id пишет в `po:proactive`
- **Unit**: `publish_callback_event` с user_id/project_id
- **Unit**: `_listen_proactive` в telegram bot
- **Integration**: engineering trigger → po:input event → PO graph → po:proactive → telegram

### 2.4 Reminders: `set_reminder` tool

PO может попросить разбудить себя через N минут (после trigger_engineering — проверить статус):

```python
@tool
async def set_reminder(user_id: str, delay_minutes: int, reason: str) -> str:
    """Set a reminder to check back after a delay.

    Args:
        user_id: User to remind about
        delay_minutes: Minutes until reminder fires
        reason: What to check (e.g., "check engineering task eng-abc123")
    """
    fire_at = time.time() + delay_minutes * 60
    await redis.zadd("po:reminders", {json.dumps({
        "type": "reminder",
        "user_id": user_id,
        "text": reason,
        "timestamp": datetime.now(UTC).isoformat(),
    }): fire_at})
    return f"Reminder set for {delay_minutes} minutes"
```

**Reminder poller** — asyncio task в consumer, проверяет `po:reminders` sorted set каждые 30 секунд. Забирает записи с `score <= now()`, публикует в `po:input`.

```python
async def _poll_reminders():
    """Move due reminders from po:reminders ZSET to po:input stream."""
    while True:
        now = time.time()
        due = await redis.zrangebyscore("po:reminders", 0, now)
        for entry in due:
            data = json.loads(entry)
            await redis.xadd("po:input", data)
            await redis.zrem("po:reminders", entry)
        await asyncio.sleep(30)
```

### 2.5 Переходный период: `cli-agent:user-messages`

Пока Developer ещё использует `orchestrator respond` → пишет в `cli-agent:user-messages`. На переходный период: listener маршрутизирует эти сообщения в `po:input` как system events, а не напрямую в Telegram.

После полного перевода Developer на system events — `cli-agent:user-messages` удаляется (фаза 3).

---

## Фаза 3: Cleanup

1. Убрать PO-специфичный код из worker-manager (тип `po`)
2. Убрать `session:po:{user_id}`, `worker:{id}:input/output` Redis keys
3. orchestrator-cli: оставить только Developer/Tester tools
4. Убрать `cli-agent:user-messages` stream (когда Developer перейдёт на events)
5. Убрать `progress:po:{user_id}:{uuid}` streams (Telegram bot больше не слушает их напрямую)
6. Обновить E2E тесты

---

## Future

### Summarization (Post-MVP)

Вместо тупой обрезки старых сообщений — сворачивать в summary. Раз в N сообщений (или при приближении к лимиту) отдельный LLM-вызов сжимает историю, сохраняя ключевые решения, факты о проекте и предпочтения пользователя. Summary пиннится как `SystemMessage` в начале контекста.

### RAG: доступ к документации и логам

PO должен видеть текущее состояние проекта. Дополнительные tools:
- `read_file(path)` — README, ARCHITECTURE.md
- `list_api_endpoints()` — через OpenAPI
- `read_recent_logs(service, lines=50)` — диагностика без контейнера

### Vector DB: долгосрочная память

Пользователи возвращаются через месяцы. Vector Store для embedding'ов всех сообщений и summary. Retrieval по истории всех тредов пользователя.

### Model cascade

Intent classifier (Haiku) → router → Sonnet/Opus. Добавляется как нода в граф.

### Dynamic configs

Смена модели/промпта из БД — мгновенно, без respawn. Основа — `agent_configs` таблица.

### Admin UI

PO-граф expose-ит state через API — conversation history, active tools, cost tracking.

### Rate limiting / cost tracking

Middleware в LangGraph, считает tokens per user. Базовая защита от спама.

---

## Риски и митигация

### 1. Потеря Claude Code subscription pricing

**Риск**: Claude Code Max = фиксированная цена за seat. API = per-token.

**Митигация**: PO — координатор, не кодер. Сообщения короткие. Оценка:
- 20 users × 10 msg/day × ~1K tokens/msg = ~200K tokens/day
- Sonnet: ~$3/M input, ~$15/M output = ~$5-10/day = ~$150-300/month
- С prompt caching: ~$50-100/month
- Claude Code Max seat: $100-200/month

Стоимость сопоставима, но масштабируется линейно vs фиксированно на seat.

### 2. Vendor lock-in

**Митигация**: LangGraph абстрагирует модель. `ChatAnthropic` заменяется на `ChatOpenAI` одной строкой. OpenRouter даёт доступ ко всем провайдерам.

### 3. Conversation compaction

**Риск**: Нужно самим управлять длиной контекста.

**Митигация**: `trim_messages` в state_modifier. Для PO достаточно последних 20-30 сообщений. Summarization — future.

### 4. Потеря Claude Code features

**Риск**: PO теряет доступ к file editing, codebase search, MCP.

**Митигация**: PO **никогда не использовал** эти фичи. Инструкция: "You are NOT a coding agent. NEVER write code yourself." Весь функционал — CLI-команды, которые заменяются tools.

### 5. LLM API downtime

**Риск**: OpenRouter или модель недоступна.

**Митигация**: Consumer ловит ошибку, отправляет пользователю "Сервис временно недоступен, попробуйте через пару минут". Сообщение ACK'd — при recovery не будет повторной обработки. Fallback на другую модель — future (dynamic configs).

### 6. Идемпотентность при retry

**Риск**: Consumer упал после `ainvoke` но до `xack` — сообщение обработается дважды. PO создаст дубликат проекта.

**Митигация**: Для MVP — `xack` в `finally` минимизирует окно. Для production — `create_project` проверяет дубли по имени, `trigger_engineering` — по project_id + cooldown.

---

## Лог решений

| Вопрос | Решение | Обоснование |
|--------|---------|-------------|
| Где живёт PO-граф? | `services/langgraph/src/po/`, asyncio task в основном контейнере | Контейнер недогружен, вынос в отдельный — при росте нагрузки |
| Redis streams | Один общий `po:input` с полем `user_id` | Нет проблемы discovery стримов, один consumer group, проще мониторинг |
| Concurrent users | `asyncio.Semaphore(10)` + per-user `asyncio.Lock` | Юзеры не блокируют друг друга, сообщения одного юзера — последовательно |
| Telegram → PO | `XADD po:input` + `XREAD po:response:{request_id}` | Паттерн идентичен текущему, без worker-manager |
| Typing indicator | `po:ack:{request_id}` + периодический `send_chat_action` | UX-критично при 5-20 сек обработки |
| Conversation history | PostgreSQL checkpointer + `trim_messages` (один thread навсегда) | Пользователь не управляет контекстом |
| LLM провайдер | OpenRouter (один ключ, все модели) | Фаза экспериментов, быстрая смена моделей |
| Env vars | Без дефолтов, `RuntimeError` если не заданы | Правило проекта (CLAUDE.md) |
| PO tools | Новые async-функции с shared httpx/redis client | orchestrator-cli остаётся для Developer/Tester |
| System events | Все через PO. PO — единственная точка коммуникации с пользователем | Фильтрация, перевод ошибок, автономные действия |
| Proactive messages | Один общий стрим `po:proactive` с полем `user_id`, consumer group `tg-bot` | Консистентно с `po:input`, один XREADGROUP, нет discovery при рестарте, масштабируется на несколько bot-инстансов |
| Reminders | `ZADD po:reminders` + poller каждые 30 сек | Простая реализация без внешних зависимостей (APScheduler, Celery) |
| Telegram → PO (Phase 2.1) | Direct XADD/XREAD, no container | 4-hop path replaces 7-hop; typing indicator included (Phase 2.2 merged — starts immediately after XADD, no ACK stream needed); clean cut (no feature flag) since not in production |
| Миграция сессий | Чистый лист | Мы не в проде |
| Graceful shutdown | Перестаём читать, ждём текущие tasks, ACK | Сообщения не обработаются дважды при рестарте |
| Checkpointer (Phase 1) | `MemorySaver` — in-memory | Быстрый старт, не нужен PostgreSQL для разработки |
| Checkpointer (Phase 1.5) | `AsyncPostgresSaver` в отдельной schema `langgraph` (**implemented**) | Та же БД, Alembic не пересекается (работает с `public`), отдельный `CHECKPOINT_DATABASE_URL` env var. Schema создаётся psycopg3 sync перед `setup()`. MemorySaver fallback если env var не задан. |
| `create_react_agent` prompt API | `prompt=callable` (не `state_modifier`) | langgraph-prebuilt 1.0.5 убрал `state_modifier`, `prompt` принимает callable(state) -> messages |
| How PO notifies user on system events | `notify_user` tool (explicit) | PO calls it only when needed. No magic markers, no "always forward". PO can stay silent by simply not calling the tool. |
| Event format in `publish_callback_event` | Flat fields (not JSON-wrapped `data`) | Matches `po:input` consumer expectations. Old JSON format was never consumed by anyone. |
| user_id in PO tools | `RunnableConfig` injection via `config` kwarg | LangChain standard. Consumer sets `configurable.user_id`, tools read it. No `InjectedState`. |
| callback_stream | `PO_INPUT_QUEUE` (constant) | Single stream, no discovery problem. Workers write same format as always — they don't know about PO. |
| ProactiveListener | Follows ProvisionerNotifier pattern | Consumer group, XREADGROUP, same startup/shutdown lifecycle. Proven pattern in codebase. |
| Reminder poller (Phase 2.4) | Standalone async coroutine with `_poll_once` + loop | `_poll_once` is testable independently. Own Redis connection (separate from consumer). `ZRANGEBYSCORE` + `ZREM` not atomic — acceptable for single-process; Lua/ZPOPMIN if we scale. `PO_REMINDERS_KEY` constant extracted to `shared/queues.py`. |
