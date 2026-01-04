# Persistent Agents MVP Implementation Plan

**Цель**: Реализовать универсальную систему persistent CLI-агентов с tool-based communication через **единый bash скрипт `orchestrator`**, поддерживающий любые типы агентов (Claude, Codex, Factory.ai, Gemini CLI).

**Статус**: Planning
**Дата создания**: 2026-01-04
**Последнее обновление**: 2026-01-04

---

## Ключевая идея

**Все CLI агенты имеют bash** → все вызывают `orchestrator answer "text"` → скрипт сам пишет в Redis/HTTP → **полиморфизм на уровне shell команд!**

**Никакого парсинга stdout для tool calls!** Агент просто выводит логи, а tool calls делает через bash команды.

---

## Проблемы текущей архитектуры

### ❌ Что не так сейчас:

1. **Ephemeral процессы** - каждое сообщение = новый процесс, история теряется
2. **Output-based communication** - парсим JSON из stdout (неправильно!)
3. **Session management complexity** - храним session_id в Redis
4. **Container readiness race** - команда отправляется до завершения entrypoint
5. **Отсутствие абстракции** - код завязан на конкретный CLI агент

### ✅ Целевая архитектура MVP:

1. **Persistent процессы** - один процесс на TTL контейнера (2 часа)
2. **Tool-based communication через единый CLI** - `orchestrator answer "text"`
3. **Bash скрипт сам публикует** - в Redis/HTTP, никакого парсинга!
4. **stdout/stderr = чистые логи** - никакой бизнес-логики
5. **Agent abstraction** - единый скрипт для всех агентов

---

## MVP Scope

**Делаем:**
- ✅ Единый `orchestrator` CLI для всех агентов
- ✅ Persistent процессы (stdin/stdout)
- ✅ Логирование в Redis
- ✅ Graceful shutdown
- ✅ Поддержка Claude Code и Factory Droid

**НЕ делаем:**
- ❌ Парсинг stdout для tool calls
- ❌ BMAD-структура
- ❌ Agent-to-agent communication
- ❌ Context compaction

---

## Архитектура

```
User Message
    ↓
ProcessManager.write_to_stdin(agent_id, "Create project myapp")
    ↓
Agent (Claude/Factory/Codex) обрабатывает
    ↓
Agent вызывает bash: orchestrator answer "Done! Project created."
    ↓
/usr/local/bin/orchestrator (bash script)
    ↓
redis-cli XADD cli-agent:responses ... ИЛИ curl -X POST api:8000/tools/answer
    ↓
Telegram Bot читает из Redis
```

**Ключевое отличие**: Скрипт orchestrator сам отправляет данные, ProcessManager просто читает stdout как логи!

---

## Компоненты

### 1. AgentFactory (Упрощённый!)

**УБРАЛИ:**
- ~~`get_tool_call_pattern()`~~ - не нужен!
- ~~`parse_tool_call()`~~ - не нужен!

**Оставили:**
```python
class AgentFactory(ABC):
    @abstractmethod
    def get_persistent_command(self) -> str:
        """Claude: 'claude --dangerously-skip-permissions'"""

    @abstractmethod
    def format_message_for_stdin(self, message: str) -> str:
        """Claude: f'{message}\\n'"""

    @abstractmethod
    def generate_instructions(self, allowed_tools) -> dict[str, str]:
        """Генерация CLAUDE.md/AGENTS.md с примерами orchestrator CLI"""
```

### 2. Orchestrator CLI Script (КЛЮЧЕВОЙ КОМПОНЕНТ!)

**`/usr/local/bin/orchestrator`** - единый скрипт для ВСЕХ агентов:

```bash
#!/bin/bash
set -euo pipefail

AGENT_ID="${ORCHESTRATOR_AGENT_ID}"
REDIS_URL="${ORCHESTRATOR_REDIS_URL:-redis://redis:6379}"
API_URL="${ORCHESTRATOR_API_URL:-http://api:8000}"

COMMAND="$1"
shift || true

# Телеметрия
log_tool_call() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] TOOL_CALL: $1 agent_id=$AGENT_ID" >&2
}

case "$COMMAND" in
    answer)
        MESSAGE="$1"
        log_tool_call "answer"

        # Публикуем в Redis
        redis-cli -u "$REDIS_URL" XADD "cli-agent:responses" "*" \
            "agent_id" "$AGENT_ID" \
            "type" "answer" \
            "message" "$MESSAGE" \
            "timestamp" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null

        echo "Answer sent" >&2
        ;;

    ask)
        QUESTION="$1"
        log_tool_call "ask"

        redis-cli -u "$REDIS_URL" XADD "cli-agent:responses" "*" \
            "agent_id" "$AGENT_ID" \
            "type" "question" \
            "question" "$QUESTION" >/dev/null

        echo "Question sent to user" >&2
        ;;

    project)
        # orchestrator project create --name myapp
        SUBCOMMAND="$1"
        shift

        # Парсим --name, --id и т.д.
        # Вызываем curl -X POST api:8000/projects
        ;;

    *)
        echo "Unknown command: $COMMAND" >&2
        exit 1
        ;;
esac
```

**Преимущества:**
- ✅ Телеметрия встроена (`log_tool_call`)
- ✅ Retry logic (можно добавить)
- ✅ Validation аргументов
- ✅ Единый формат для всех агентов
- ✅ Прозрачность (легко дебажить)

### 3. ProcessManager (БЕЗ ИЗМЕНЕНИЙ)

```python
class ProcessManager:
    async def start_process(self, agent_id: str, factory: AgentFactory):
        command = factory.get_persistent_command()
        # Start docker exec -i agent_id /bin/bash -l -c command
        # ...

    async def write_to_stdin(self, agent_id: str, message: str):
        formatted = factory.format_message_for_stdin(message)
        stdin.write(formatted.encode())

    async def read_stdout_line(self, agent_id: str) -> str | None:
        # Просто читаем логи!
```

### 4. LogCollector (УПРОЩЁН!)

**Никакого парсинга tool calls - только логи!**

```python
class LogCollector:
    async def start_collecting(self, agent_id: str, process_manager):
        while self._listening[agent_id]:
            line = await process_manager.read_stdout_line(agent_id)
            if line:
                await self._store_log(agent_id, "stdout", line)

    async def _store_log(self, agent_id, stream, line):
        await redis.xadd(f"agent:logs:{agent_id}", {
            "stream": stream,
            "line": line,
            "timestamp": datetime.now(UTC).isoformat()
        }, maxlen=1000)
```

### 5. ToolCallListener?

**НЕ НУЖЕН!** Убрали полностью.

Скрипт orchestrator сам отправляет данные в Redis, не нужно перехватывать tool calls из stdout.

---

## Пример работы

**1. Пользователь:** "Create project myapp"

**2. ProcessManager:**
```python
await process_manager.write_to_stdin(agent_id, "Create project myapp")
```

**3. Claude обрабатывает:**
```
Thinking: I need to create a project...
Let me use the orchestrator CLI.
```

**4. Claude вызывает bash:**
```bash
orchestrator project create --name myapp
```

**5. Скрипт orchestrator:**
```bash
# Телеметрия
[2026-01-04T15:30:45Z] TOOL_CALL: project.create agent_id=agent-abc123

# HTTP запрос к API
curl -X POST http://api:8000/projects -d '{"name":"myapp"}'

# Ответ
{"id": "proj_456"}
```

**6. Claude продолжает:**
```
Perfect! Project created.
```

**7. Claude вызывает:**
```bash
orchestrator answer "Done! Created project 'myapp' (ID: proj_456)."
```

**8. Скрипт orchestrator:**
```bash
# Публикует в Redis
redis-cli XADD cli-agent:responses * \
    agent_id agent-abc123 \
    type answer \
    message "Done! Created..."
```

**9. Telegram Bot:**
```python
message = await redis.xread({"cli-agent:responses": last_id})
await bot.send_message(user_id, message["message"])
```

**Ключевое**: Все агенты используют ОДИНАКОВЫЙ скрипт! Полиморфизм на bash уровне!

---

## Фазы реализации

### Phase 0: Design (1-2 дня)

**Задачи:**
1. Упростить `AgentFactory` - убрать парсинг методы
2. Спроектировать orchestrator CLI скрипт
3. Определить environment variables (ORCHESTRATOR_AGENT_ID, REDIS_URL, API_URL)
4. Обновить ProcessManager API (без изменений)
5. Упростить LogCollector (только логи, без парсинга)

**Критерии:**
- [ ] Все интерфейсы определены
- [ ] Orchestrator CLI спроектирован
- [ ] Примеры для Claude и Factory

### Phase 1: AgentFactory Extensions (1-2 дня)

**Задачи:**
1. Обновить базовый класс (убрать get_tool_call_pattern, parse_tool_call)
2. Реализовать ClaudeCodeAgent persistent методы
3. Реализовать FactoryDroidAgent persistent методы
4. Unit тесты

**Критерии:**
- [ ] AgentFactory упрощён
- [ ] Claude и Factory реализованы
- [ ] Тесты покрывают >90%

### Phase 2: Orchestrator CLI Script (2 дня)

**Задачи:**
1. Написать bash скрипт с командами: answer, ask, project, deploy, engineering, infra
2. Добавить телеметрию, retry, validation
3. Обновить Dockerfile universal-worker (установить redis-cli, jq)
4. Integration тесты скрипта

**Критерии:**
- [ ] Скрипт реализован
- [ ] Dockerfile обновлён
- [ ] Integration тесты проходят

### Phase 3: ProcessManager (2-3 дня)

**Задачи:**
1. Создать ProcessManager (без изменений от дизайна)
2. Интеграция с ContainerService (установить env vars)
3. Unit/integration тесты

**Критерии:**
- [ ] ProcessManager работает
- [ ] Env vars правильно установлены
- [ ] Тесты проходят

### Phase 4: LogCollector (1 день)

**Задачи:**
1. Создать упрощённый LogCollector (только логи!)
2. Unit/integration тесты

**Критерии:**
- [ ] LogCollector собирает логи
- [ ] Тесты проходят

### Phase 5: Integration (2 дня)

**Задачи:**
1. Обновить Redis handlers (create, send_message, delete)
2. Dependency injection
3. E2E тесты (Claude + Factory)

**Критерии:**
- [ ] Redis handlers обновлены
- [ ] E2E тесты проходят
- [ ] Claude и Factory работают одинаково!

### Phase 6: API & Observability (1-2 дня)

**Задачи:**
1. API endpoint `/agents/{agent_id}/logs`
2. Structured logging
3. Health check endpoint

**Критерии:**
- [ ] API работает
- [ ] Логи структурированы
- [ ] Health check отвечает

### Phase 7: Testing & Stabilization (2-3 дня)

**Задачи:**
1. E2E тестирование через Telegram
2. Stress тесты (10+ агентов)
3. Error scenarios (Redis down, etc.)
4. Документация

**Критерии:**
- [ ] E2E проходит
- [ ] Stress выдержан
- [ ] Errors обработаны
- [ ] Docs обновлены

### Phase 8: Rollout (1 день)

**Задачи:**
1. Docker образы
2. Staging deployment
3. Production deployment
4. Announcement

---

## Timeline

| Phase | Duration |
|-------|----------|
| 0. Design | 1-2 дня |
| 1. AgentFactory | 1-2 дня |
| 2. Orchestrator CLI | 2 дня |
| 3. ProcessManager | 2-3 дня |
| 4. LogCollector | 1 день |
| 5. Integration | 2 дня |
| 6. API & Observability | 1-2 дня |
| 7. Testing | 2-3 дня |
| 8. Rollout | 1 день |

**Total**: 13-18 дней (~2.5-3.5 недели)

С учётом параллельной работы: **~2.5 недели**.

---

## Success Criteria

**Functional:**
- [ ] Любой CLI агент работает через orchestrator
- [ ] Persistent процессы живут 2+ часа
- [ ] Tool-based communication работает
- [ ] Логи собираются
- [ ] No session_id
- [ ] Graceful shutdown

**Non-Functional:**
- [ ] Response time <30 сек
- [ ] Uptime >99%
- [ ] Support 10+ агентов
- [ ] Code coverage >90%
- [ ] Добавление агента <1 дня

**Business:**
- [ ] Claude и Factory работают одинаково
- [ ] Документация полная
- [ ] Пользователь не видит разницы между агентами

---

## Risks & Mitigation

**Risk 1: Redis недоступен**
- Mitigation: Retry в скрипте, fallback на HTTP

**Risk 2: Agent не использует orchestrator**
- Mitigation: Чёткие инструкции в CLAUDE.md, примеры

**Risk 3: Скрипт медленный**
- Mitigation: Профилирование, возможно замена redis-cli на netcat

---

## Future Enhancements

1. **Orchestrator SDK** - Python/Node.js библиотеки вместо bash
2. **WebSocket для ask/answer** - bidirectional communication
3. **Context Window Management** - compaction, summaries
4. **Multi-Agent Communication** - `orchestrator route_to_agent`

---

## Appendix

### A. Environment Variables

Устанавливаются workers-spawner при создании контейнера:

```bash
ORCHESTRATOR_AGENT_ID=agent-abc123
ORCHESTRATOR_REDIS_URL=redis://redis:6379
ORCHESTRATOR_API_URL=http://api:8000
```

### B. Orchestrator Commands

```bash
orchestrator answer "message"
orchestrator ask "question"
orchestrator project create --name myapp
orchestrator project get --id proj_123
orchestrator deploy --project-id proj_123
orchestrator engineering --task "..." --project-id proj_123
orchestrator infra --task "..." --server-id srv_456
```

### C. Redis Streams

**Ответы:**
```
Stream: cli-agent:responses
Fields: agent_id, type (answer|question), message/question, timestamp
```

**Логи:**
```
Stream: agent:logs:{agent_id}
Fields: stream (stdout|stderr), line, timestamp
Retention: Last 1000 lines
```

### D. Comparison

| Aspect | Old (Ephemeral) | MVP (Persistent + orchestrator CLI) |
|--------|-----------------|--------------------------------------|
| Process | 1 per message | 1 per container (2h) |
| Tool calls | Parsed from stdout JSON | Bash commands → Redis |
| Stdout | Business logic | Pure logs |
| Complexity | High (парсинг, ToolCallListener) | Low (bash скрипт) |
| Полиморфизм | Нет | Да (bash level) |
| Latency | ~5-10s | <1s |

---

## Conclusion

Упрощённый MVP план с **единым CLI скриптом** для всех агентов.

**Ключевые преимущества:**
1. ✅ **Проще архитектура** - нет парсинга stdout, нет ToolCallListener
2. ✅ **Телеметрия встроена** - скрипт логирует каждый вызов
3. ✅ **Полиморфизм на bash уровне** - все вызывают `orchestrator`
4. ✅ **Легко добавить агента** - нужен только bash
5. ✅ **Меньше кода** - убрали сложную логику парсинга
6. ✅ **Надёжнее** - скрипт делает retry, validation

**Результат**: Система где все CLI агенты (Claude, Factory, Codex, Gemini) используют один интерфейс `orchestrator`, и детали реализации полностью скрыты.
