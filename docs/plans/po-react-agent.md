# Plan: PO как LangGraph ReactAgent (без контейнера)

> **Дата**: 2026-02-15
> **Статус**: Design Ready
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
  User → Telegram → Redis (po:input:{user_id})
    → PO consumer (в langgraph контейнере) → ainvoke(PO graph) → tools (API/Redis)
    → Redis (po:response:{request_id}) → Telegram → User
```

### Где живёт PO consumer

PO consumer — лёгкий event handler внутри основного `langgraph` контейнера. Добавляется как ещё один asyncio task:

```python
# services/langgraph/src/worker/main.py
async def run_worker():
    await asyncio.gather(
        listen_provisioner_triggers(),   # существующий pub/sub listener
        listen_worker_events(),          # существующий pub/sub listener
        run_po_consumer(),               # новое: stream consumer для PO
    )
```

Основной `langgraph` контейнер сейчас недогружен (два лёгких pub/sub listener'а). PO consumer — такой же лёгкий: читает Redis stream → ainvoke (секунды) → пишет ответ. Если нагрузка вырастет — выносим в отдельный контейнер с отдельным CMD, код менять не нужно.

### PO как LangGraph граф

```python
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

# Модель (через OpenRouter — один ключ, все провайдеры)
model = ChatOpenAI(
    model="anthropic/claude-sonnet-4-5",
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Tools — прямые Python-функции вместо CLI-команд
tools = [
    create_project,        # POST /api/projects/
    list_projects,         # GET /api/projects/
    get_project,           # GET /api/projects/{id}
    set_project_secret,    # PATCH /api/projects/{id}
    trigger_engineering,   # POST /api/tasks/ + XADD engineering:queue
    trigger_deploy,        # POST /api/tasks/ + XADD deploy:queue
    get_task_status,       # GET /api/tasks/{id}
    set_reminder,          # Scheduler: разбуди PO через N минут
]

# Граф
po_graph = create_react_agent(
    model=model,
    tools=tools,
    state_modifier=system_prompt,  # Из INSTRUCTIONS.md
)
```

### Conversation history — PostgreSQL checkpointer

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

checkpointer = AsyncPostgresSaver(conn_string=DATABASE_URL)

# Каждый пользователь = отдельный thread (навсегда, без сессий)
config = {"configurable": {"thread_id": f"po-user-{user_id}"}}
result = await po_graph.ainvoke({"messages": [user_message]}, config=config)
```

Заменяет Claude Code `--resume`. LangGraph сам управляет историей, хранит в PostgreSQL. Длина контекста контролируется через auto-trimming (пользователь не знает про контекстное окно).

### Коммуникация через Redis streams

**Единый input stream на пользователя** — принимает все типы триггеров:

```
po:input:{user_id}
  ├── {type: "user_message", text: "создай бота", request_id: "abc", ts: "..."}
  ├── {type: "system_event", event: "engineering_completed", project_id: "xyz", ...}
  ├── {type: "reminder", reason: "check task eng-123", ...}
  └── {type: "user_message", text: "как дела?", request_id: "def", ts: "..."}
```

**Response stream** — ответ PO (если есть) уходит в stream с request_id:

```
po:response:{request_id}
  └── {text: "Бот готов! Вот ссылка: ...", user_id: 123}
```

Telegram bot пишет в input stream и ждёт ответ на response stream через `xread`. Паттерн идентичен текущему взаимодействию с worker-manager.

**Correlation ID**: `request_id` проходит через всю цепочку — от Telegram до tool call и обратно. Structlog + request_id = сквозная телеметрия.

**Последовательная обработка без Lock.** Redis stream гарантирует порядок. PO consumer читает сообщения по одному. Три быстрых сообщения от пользователя → три записи в stream → consumer обрабатывает последовательно. `asyncio.Lock` не нужен — Redis stream IS the lock.

**Persistence и replay.** Redis stream хранит историю. Если PO consumer упал — при рестарте продолжит с последнего acknowledged сообщения (consumer groups).

---

## Что меняется, что остаётся

### Убираем из PO flow

| Компонент | Роль сейчас | Что с ним |
|-----------|------------|-----------|
| worker-manager (для PO) | Создаёт PO-контейнер | **Не нужен для PO.** Остаётся для Developer/Tester |
| worker-wrapper | Запускает Claude CLI в контейнере | **Не нужен для PO.** Остаётся для Developer/Tester |
| orchestrator-cli (PO tools) | CLI-прокси к API/Redis | **Заменяется** Python-функциями (tools). CLI остаётся для Developer |
| Docker-контейнер PO | Изоляция | **Не нужен.** PO не работает с FS |
| `session:po:{user_id}` → worker_id | Маппинг user → container | **Заменяется** на `thread_id` в LangGraph checkpointer |
| `worker:{id}:input/output` streams | Коммуникация с контейнером | **Заменяются** на `po:input:{user_id}` и `po:response:{request_id}` |
| `cli-agent:user-messages` stream | PO → пользователь | **Не нужен.** Ответ через `po:response:{request_id}` |

### Остаётся без изменений

| Компонент | Почему |
|-----------|--------|
| `engineering:queue` | Engineering subgraph слушает эту очередь. PO tools пишут туда напрямую |
| `deploy:queue` | Deploy subgraph слушает эту очередь |
| API endpoints (`/api/projects/`, `/api/tasks/`) | PO tools вызывают их напрямую через httpx |
| Progress events (`progress:po:{user_id}:{uuid}`) | Telegram bot слушает их. Engineering subgraph пишет. PO не участвует |
| Developer/Tester containers | Остаются в Docker (работают с FS) |
| worker-manager (для Developer) | Остаётся |

---

## PO tools и orchestrator-cli

### Решение: PO tools пишутся заново, orchestrator-cli не трогаем

PO не может использовать orchestrator-cli — у него нет CLI, нет subprocess. Переиспользовать async-функции из CLI тоже не стоит:

1. **Паттерны разные.** CLI создаёт httpx/redis клиент на каждый вызов и закрывает (`async with ... finally: await client.aclose()`). PO хочет shared client — за один invoke PO может вызвать 3-4 tools подряд (create_project → set_secret → trigger_engineering). Создавать/закрывать клиент каждый раз — лишний overhead.

2. **Логики мало.** 7 функций × ~15 строк = ~100 строк тривиального кода (HTTP POST + XADD). Писать 10 минут. Не стоит coupling ради 100 строк.

3. **orchestrator-cli остаётся для Developer/Tester.** Два независимых набора tools для двух типов агентов: CLI tools для контейнерных агентов (Developer, Tester), Python tools для API-агентов (PO, будущий Diagnostician).

### PO tools: прямые async-функции с shared client

```python
from langchain_core.tools import tool

# Shared client — создаётся один раз при старте consumer'а, инжектится в tools
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

### Полный список PO tools

| Tool | Операция | Источник данных |
|------|----------|----------------|
| `create_project` | `POST /api/projects/` | API |
| `list_projects` | `GET /api/projects/` | API |
| `get_project` | `GET /api/projects/{id}` | API |
| `set_project_secret` | `PATCH /api/projects/{id}` | API |
| `trigger_engineering` | `POST /api/tasks/` + `XADD engineering:queue` | API + Redis |
| `trigger_deploy` | `POST /api/tasks/` + `XADD deploy:queue` | API + Redis |
| `get_task_status` | `GET /api/tasks/{id}` | API |
| `set_reminder` | Scheduler: пишет в `po:input:{user_id}` через N минут | Redis |

---

## Управление историей разговора

### Проблема: контекст растёт

С каждым сообщением пользователя контекст увеличивается. При 50+ сообщениях (проект создан, потом фичи, потом баги) — контекст переполнится.

### Решение: message trimming + summarization

```python
from langgraph.prebuilt import create_react_agent

def trim_messages(messages, max_tokens=50_000):
    """Keep system prompt + last N messages that fit in token budget."""
    # Всегда сохраняем system prompt
    system = [m for m in messages if m.type == "system"]
    rest = [m for m in messages if m.type != "system"]

    # Берём последние сообщения, пока влезают
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
    state_modifier=trim_messages,  # Автоматический trimming
)
```

Для MVP достаточно trimming. Summarization (LLM сжимает старые сообщения в summary) — Post-MVP.

---

## Интеграция с Telegram Bot

### Текущий flow в telegram_bot

```python
# services/telegram_bot/src/main.py (handle_message)

# 1. Get or create PO worker container
worker_id = await session_manager.get_or_create_worker(user_id)

# 2. Send message to worker via Redis stream
request_id = await session_manager.send_message(user_id, text, callback_stream)

# 3. Listen for response on worker output stream
# (background task, _listen_for_worker_responses)
```

### Новый flow

```python
# services/telegram_bot/src/main.py (handle_message)

request_id = str(uuid.uuid4())
timestamp = datetime.now(UTC).isoformat()

# 1. Publish to PO input stream (с timestamp)
await redis.xadd(f"po:input:{user_id}", {
    "type": "user_message",
    "text": text,
    "request_id": request_id,
    "timestamp": timestamp,
})

# 2. Wait for response on dedicated response stream
response = await redis.xread(
    {f"po:response:{request_id}": "$"},
    block=30000,  # 30s timeout
)

# 3. Send to user
await context.bot.send_message(chat_id, response.text)
```

### POSessionManager → тонкий Redis publisher

`POSessionManager` с логикой создания контейнеров **заменяется** минимальной обёрткой: publish в Redis stream + wait на response stream. Паттерн идентичен текущему, но без worker-manager в середине.

**Concurrent messages решаются Redis stream'ом.** Пользователь шлёт 3 сообщения подряд — все попадают в `po:input:{user_id}` stream по порядку. PO consumer читает по одному, обрабатывает последовательно. Никаких `asyncio.Lock`, никакого batching — Redis stream IS the queue.

**Timestamp в каждом сообщении.** Каждое сообщение содержит UTC timestamp. PO видит временной контекст:
- Между "Хочу бота" и "Вот токен" прошло 10 минут → пользователь ходил в BotFather
- Между двумя сообщениями 2 секунды → пользователь дробит мысль, это одно сообщение
- При message trimming timestamps дают контекст "это было вчера"

### PO consumer: обработка сообщений

```python
# services/langgraph/src/po/consumer.py

async def run_po_consumer():
    """Main loop: read from po:input:* streams, invoke PO graph, write response."""
    while True:
        # Читаем из всех po:input:* стримов (по одному сообщению)
        streams = await discover_active_streams()  # po:input:*
        messages = await redis.xread(streams, count=1, block=5000)

        for stream_name, entries in messages:
            for msg_id, data in entries:
                user_id = extract_user_id(stream_name)  # po:input:{user_id} → user_id

                # Форматируем сообщение с timestamp
                formatted = f"[{data['timestamp']} UTC] {data['text']}"

                # Определяем тип сообщения для LangGraph
                if data["type"] == "user_message":
                    msg = HumanMessage(content=formatted)
                else:  # system_event, reminder
                    msg = SystemMessage(content=formatted)

                # Invoke PO graph
                result = await po_graph.ainvoke(
                    {"messages": [msg]},
                    config={"configurable": {"thread_id": f"po-user-{user_id}"}},
                )

                response_text = result["messages"][-1].content

                # Пишем ответ в response stream (если есть request_id)
                if data.get("request_id"):
                    await redis.xadd(f"po:response:{data['request_id']}", {
                        "text": response_text,
                        "user_id": str(user_id),
                    })

                # Для system events PO может решить промолчать
                # (не писать в response stream) или отправить
                # сообщение пользователю через отдельный stream

                # ACK
                await redis.xack(stream_name, "po-consumer", msg_id)
```

---

## PO как event-driven actor

### Принцип: PO не живёт — он вызывается

PO не держит открытое соединение с LLM. Каждый вызов — короткий (секунды), stateless между вызовами. State хранится в checkpointer. Три типа триггеров вызывают PO:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        ТРИГГЕРЫ PO                                      │
├──────────────────┬──────────────────────────────────────────────────────┤
│                  │                                                      │
│  User message    │  Telegram → POService → ainvoke(PO)                  │
│                  │  "создай бота", "как дела?"                          │
│                  │                                                      │
│  System event    │  Engineering/Deploy subgraph → event → ainvoke(PO)   │
│                  │  task_completed, task_failed, scaffolding_done        │
│                  │                                                      │
│  Timer           │  Scheduler → ainvoke(PO)                             │
│                  │  "проверь статус через 10 минут"                     │
│                  │                                                      │
└──────────────────┴──────────────────────────────────────────────────────┘
```

### Пример полного workflow

```
t=0s    User: "Создай бота для пиццерии, вот токен: 123:ABC"
        → ainvoke(PO)
        → PO: create_project() → set_secret() → trigger_engineering()
        → PO → User: "Начал разработку! Напишу когда будет готово."
        → done (3 сек, соединение закрыто)

t=30s   Event: scaffolding_completed
        → ainvoke(PO, system_message="Scaffolding завершён для проекта X")
        → PO решает: пользователю неинтересны технические детали → молчит
        → done (1 сек)

t=5m    Event: developer_started
        → ainvoke(PO, system_message="Developer начал работу")
        → PO решает: можно сообщить → User: "Агент начал писать код"
        → done (1 сек)

t=15m   Event: engineering_completed {status: "success", url: "..."}
        → ainvoke(PO, system_message="Engineering завершён, URL: ...")
        → PO → User: "Бот готов! Вот ссылка: https://..."
        → done (2 сек)

        ИЛИ

t=12m   Event: engineering_failed {error: "pytest failed: ImportError..."}
        → ainvoke(PO, system_message="Engineering failed: ...")
        → PO решает: перезапустить или сообщить пользователю
        → PO: trigger_engineering(retry=True) ИЛИ → User: "Возникла проблема, пробую исправить"
        → done (2 сек)
```

Каждый вызов — секунды. Между вызовами PO не существует. Нет idle-контейнера, нет открытого LLM-соединения.

### PO — единственная точка коммуникации с пользователем

**Ни один внутренний компонент не пишет пользователю напрямую.** Все события идут через PO:

```
Было:
  Engineering subgraph → progress events → Telegram (напрямую)
  Developer worker → orchestrator respond → Telegram (напрямую)
  PO → orchestrator respond → Telegram

Станет:
  Engineering subgraph → event → PO → (решает) → Telegram
  Developer worker → event → PO → (решает) → Telegram
  Timer → PO → (решает) → Telegram
```

Почему:
1. **PO решает** нужно ли пересылать пользователю. Пользователь сказал "не парь мне мозг деталями" — PO запоминает и фильтрует. Другой пользователь хочет видеть всё — PO пересылает.
2. **PO переводит** технические ошибки в человеческий язык. `ImportError in line 228` → "Возникла ошибка при сборке, пробую починить".
3. **PO может действовать** — поймать ошибку и перезапустить задачу, не беспокоя пользователя.
4. **Одна точка** — проще отлаживать, логировать, контролировать что уходит пользователю.

### Events: как PO получает системные события

Варианты доставки событий:

**A) Redis pub/sub → listener → ainvoke(PO)**

Отдельный listener-сервис (или часть telegram bot) подписан на системные события. При получении — вызывает PO с system message:

```python
# Event listener (в telegram bot или отдельный сервис)
async def on_system_event(event: SystemEvent):
    """System event triggers PO invocation."""
    system_msg = SystemMessage(
        content=f"[SYSTEM EVENT at {event.timestamp} UTC] "
                f"Type: {event.type}, Project: {event.project_id}\n"
                f"Details: {event.payload}"
    )

    result = await po_service.handle_event(
        user_id=event.user_id,
        event=system_msg,
    )

    # PO вернул ответ для пользователя
    if result.text:
        await telegram_bot.send_message(event.chat_id, result.text)
    # PO решил промолчать — ничего не отправляем
```

**B) PO tool: `set_reminder`**

PO может сам попросить разбудить его:

```python
@tool
async def set_reminder(delay_minutes: int, reason: str) -> str:
    """Set a reminder to check back after a delay.

    Use this after triggering a long-running task (engineering, deploy)
    to check on progress later.

    Args:
        delay_minutes: Minutes until reminder fires
        reason: What to check (e.g., "check engineering task eng-abc123")
    """
    await scheduler.schedule(
        trigger_po_invocation,
        delay_minutes=delay_minutes,
        user_id=context.user_id,
        message=f"[REMINDER] {reason}",
    )
    return f"Reminder set for {delay_minutes} minutes"
```

Оба варианта не исключают друг друга. Events приходят автоматически (push), reminders — по запросу PO (pull).

---

## Контекст без сессий

### Принцип: пользователь не управляет контекстом

Пользователи не разработчики. Они не знают что такое контекстное окно, tokens, sessions. Вся работа с контекстом — автоматическая, невидимая.

### Реализация: один thread навсегда + auto-trimming

```
User 123:
  thread_id = "po-user-123" (создаётся при первом сообщении, живёт вечно)

  Контекст окно:
  ┌─────────────────────────────────────────────────────┐
  │ System prompt (всегда)                               │
  │                                                      │
  │ [auto-summary старых сообщений, если есть]           │
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

**MVP**: `trim_messages()` — оставляем system prompt + последние N сообщений (по token budget). Старые просто отрезаются.

**Позже**: Auto-summarization — перед обрезкой LLM сжимает старые сообщения в compact summary. Пиннится в начале thread. PO знает что "2 недели назад создали бота для пиццерии" даже если сами сообщения уже обрезаны.

---

## Что делать с `cli-agent:user-messages` stream

Сейчас:
1. **PO** → `orchestrator respond` → `cli-agent:user-messages` → Telegram
2. **Developer** → `orchestrator respond` → `cli-agent:user-messages` → Telegram

С новой архитектурой:
1. **PO** → return value из `ainvoke()` → POService → Telegram (прямой return, без stream)
2. **Developer** → system event → PO → Telegram (PO фильтрует/переводит)

`cli-agent:user-messages` stream **не нужен**. Developer не пишет пользователю — он публикует system events. PO решает что переслать.

Но на переходный период (пока Developer ещё использует `orchestrator respond`) — listener на `cli-agent:user-messages` маршрутизирует сообщения через PO, а не напрямую в Telegram.

---

## Миграция: поэтапный план

### Фаза 1: PO Graph + Tools + Consumer

1. Создать `services/langgraph/src/po/` с:
   - `graph.py` — PO ReactAgent граф (create_react_agent + OpenRouter)
   - `tools.py` — Python tools (create_project, trigger_engineering, ...)
   - `prompts.py` — system prompt (адаптация INSTRUCTIONS.md)
   - `consumer.py` — Redis stream consumer (читает `po:input:*`, вызывает граф, пишет `po:response:*`)

2. PostgreSQL checkpointer для conversation history (auto-trimming)

3. Подключить consumer в `src/worker/main.py` (asyncio.gather)

4. Unit-тесты для каждого tool + graph integration test

### Фаза 2: Интеграция с Telegram Bot

1. Telegram bot публикует в `po:input:{user_id}` вместо `worker:commands`
2. Telegram bot ждёт ответ на `po:response:{request_id}` вместо `worker:{id}:output`
3. Убрать создание PO-контейнеров (`POSessionManager._create_worker()`)

### Фаза 3: Event-driven PO

1. Engineering/deploy workers публикуют system events в `po:input:{user_id}`
2. Tool `set_reminder` — scheduler пишет reminders в `po:input:{user_id}`
3. PO решает что пересылать пользователю, что обработать молча
4. Маршрутизация `cli-agent:user-messages` через `po:input` (переходный период)

### Фаза 4: Cleanup

1. Убрать PO-специфичный код из worker-manager (тип `po`)
2. Убрать `session:po:{user_id}`, `worker:{id}:input/output` Redis keys
3. orchestrator-cli: оставить только Developer/Tester tools
4. Убрать `cli-agent:user-messages` stream (когда Developer перейдёт на events)
5. Обновить E2E тесты

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

### 2. Vendor lock-in на Anthropic API

**Митигация**: LangGraph абстрагирует модель. `ChatAnthropic` заменяется на `ChatOpenAI` одной строкой. Можно добавить fallback.

### 3. Conversation compaction

**Риск**: Нужно самим управлять длиной контекста (Claude Code делает это автоматически).

**Митигация**: `trim_messages` в state_modifier. Для PO достаточно последних 20-30 сообщений — он координатор, не кодер. Глубокий контекст не нужен.

### 4. Потеря Claude Code features

**Риск**: PO теряет доступ к file editing, codebase search, MCP и т.д.

**Митигация**: PO **никогда не использовал** эти фичи. Его инструкция: "You are NOT a coding agent. NEVER write code yourself." Весь функционал — CLI-команды, которые заменяются tools.

### 5. Streaming (typing indicator)

**Риск**: Сейчас Claude Code стримит ответ. ReactAgent возвращает результат целиком.

**Митигация**: LangGraph поддерживает `astream_events()`. Можно стримить tool calls и финальный ответ. Для MVP достаточно "typing..." индикатора в Telegram на время обработки.

---

## Что это разблокирует

### Сразу после реализации

- **Прозрачность**: Каждый tool call виден в structlog. Можно добавить LangSmith/Langfuse позже.
- **Скорость**: Ответ PO за 1-3 секунды вместо 5-15 секунд (нет subprocess, нет container startup).
- **Тестируемость**: PO graph тестируется unit-тестами с mock tools. Сейчас E2E тест PO = поднять Docker контейнер.

### Следующие шаги, которые становятся тривиальными

- **Model cascade**: Intent classifier (Haiku) → router → Sonnet/Opus. Добавляется как нода в граф.
- **Dynamic configs**: Смена модели/промпта из БД — мгновенно, без respawn контейнера.
- **Diagnostician** (Incident Response): Тот же паттерн — LLM + read-only tools, без контейнера.
- **Admin UI**: PO-граф expose-ит state через API — можно показывать conversation history, active tools.
- **Multi-model**: Anthropic primary, OpenAI fallback — один config change.
- **Rate limiting / cost tracking**: Middleware в LangGraph, считает tokens per user.

---

## Выбор LLM-провайдера

### Варианты

| Провайдер | LangChain класс | Плюсы | Минусы |
|-----------|----------------|-------|--------|
| **OpenRouter** | `ChatOpenAI(base_url="https://openrouter.ai/api/v1")` | Один ключ, все модели, быстрая смена, OpenAI-совместимый | +50-200ms латентности, ещё одна точка отказа |
| Anthropic напрямую | `ChatAnthropic` | Минимальная латентность, полный доступ к фичам (extended thinking, prompt caching) | Только Claude, отдельный ключ |
| OpenAI напрямую | `ChatOpenAI` | GPT-4o, o3 и т.д. | Только OpenAI модели, отдельный ключ |
| Multi-provider | Несколько классов | Лучшее от каждого | Сложность, несколько ключей, разные форматы |

### Рекомендация: OpenRouter

**Для фазы экспериментов OpenRouter — оптимальный выбор.** Мы не знаем, какая модель лучше всего подходит для PO. Нужно быстро пробовать Claude Sonnet, GPT-4o, Gemini, open-source модели. Один ключ, один формат, смена модели = смена строки.

```python
from langchain_openai import ChatOpenAI

# Всё через один класс, один ключ
llm = ChatOpenAI(
    model="anthropic/claude-sonnet-4-5",  # или "openai/gpt-4o", "google/gemini-2.5-pro"
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)
```

**Стоимость**: OpenRouter не берёт наценку на inference (0% markup). Берут 5.5% при покупке кредитов. На $100/мес это ~$5.50 — цена удобства.

**Prompt caching**: Поддерживается для Claude (5 мин и 1 час TTL). System prompt PO (~1-2K tokens) будет кешироваться.

**Tool calling**: Работает через OpenAI-совместимый формат. OpenRouter транслирует в нативный формат провайдера.

**Латентность**: +50-200ms на каждый LLM-вызов (дополнительный hop через OpenRouter). Для PO (координатор, не стриминг кода) — приемлемо.

### Когда переходить на прямой API

Если после экспериментов окажется, что 90%+ запросов идут на одну модель (например, Claude Sonnet) — имеет смысл переключить на прямой `ChatAnthropic` для этой модели. OpenRouter остаётся для fallback и экспериментов.

### Конфигурация модели

Модель должна быть конфигурируемой через env var или БД, не захардкожена:

```python
# Env var для простоты
LLM_MODEL = os.getenv("PO_LLM_MODEL", "anthropic/claude-sonnet-4-5")
LLM_BASE_URL = os.getenv("PO_LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.getenv("PO_LLM_API_KEY")  # OpenRouter key

# Позже — из agent_configs в БД (dynamic configs)
```

Это сразу даёт основу для model cascade (разные модели для разных задач) и dynamic configs (Diagnostician меняет модель при incident).

---

## Принятые решения

| Вопрос | Решение |
|--------|---------|
| **Где живёт PO-граф?** | В `services/langgraph/src/po/`. PO consumer — asyncio task внутри основного `langgraph` контейнера |
| **Как telegram bot вызывает PO?** | Через Redis streams: telegram публикует в `po:input:{user_id}`, ждёт ответ на `po:response:{request_id}` |
| **Concurrent messages?** | Redis stream гарантирует порядок. Consumer обрабатывает по одному. Lock не нужен |
| **Conversation reset?** | Пользователь не управляет контекстом. Auto-trimming, один thread навсегда |
| **Миграция сессий?** | Чистый лист — мы не в проде |
| **LLM провайдер?** | OpenRouter (один ключ, все модели, OpenAI-совместимый формат) |
| **PO tools?** | Новые async-функции с shared client. orchestrator-cli не трогаем — остаётся для Developer/Tester |
| **Промежуточные события?** | Все события идут через PO. PO — единственная точка коммуникации с пользователем |
| **Timestamps?** | Каждое сообщение содержит UTC timestamp. PO видит временной контекст |

## Открытые вопросы

1. **Graceful degradation**: Если OpenRouter/LLM API лежит — что делать? Fallback на другую модель? Сообщение "сервис недоступен"? Решим по ходу реализации.

2. **System events от engineering/deploy workers**: Формат контракта для system events (какие поля, какие типы событий). Детализируем в фазе 3.

3. **System prompt**: Нужно адаптировать INSTRUCTIONS.md — убрать CLI-команды, добавить инструкции по обработке system events, описать поведение при ошибках. Детализируем в фазе 1.
