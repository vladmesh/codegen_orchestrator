# Worker Lifecycle & Communication Model

**Цель**: Воркер как абстракция "работника" — создание, коммуникация, управление ресурсами.

**Дата создания**: 2026-01-09
**Статус**: Planning

---

## Проблема

1. **Контейнеры не чистятся** — накапливаются, система виснет на 5+ воркерах
2. **Два канала коммуникации** — `orchestrator respond` vs JSON output, непонятно когда какой
3. **Нет lifecycle management** — непонятно когда воркер "работает", "ждёт", "завершил"
4. **Session management** — когда сбрасывать контекст Claude?
5. **Ресурсы** — контейнер на каждую задачу = дорого

---

## Целевая Модель: Воркер как Работник

```
Найм/онбординг  → создание контейнера + инструкции (AGENTS.md, TASK.md)
Рабочее место   → контейнер с tools (orchestrator CLI, git)
Работа          → выполнение задачи автономно
Отчётность      → JSON output / API callback
Перерыв         → PAUSED (ждёт новую задачу)
Увольнение      → TERMINATED (cleanup)
```

---

## Модель Коммуникации

### Два типа output

```
┌─────────────────────────────────────────────────────────────────┐
│                    WORKER OUTPUT MODEL                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. JSON stdout = "Я ОСТАНОВИЛСЯ"                               │
│     ─────────────────────────────────────────                   │
│     {                                                            │
│       "status": "complete" | "blocked" | "failed",              │
│       "result": {...},                                           │
│       "question": "..."  // если blocked                         │
│     }                                                            │
│                                                                  │
│     → Воркер потерял автономность                               │
│     → Контейнер переходит в PAUSED                              │
│     → Заказчик решает: ответ / новая задача / terminate         │
│                                                                  │
│  2. API call = "Я ПРОДОЛЖАЮ, вот промежуточное"                 │
│     ─────────────────────────────────────────                   │
│     POST /api/worker/progress                                    │
│     {                                                            │
│       "worker_id": "...",                                        │
│       "step": "Running tests",                                   │
│       "progress_pct": 50,                                        │
│       "output": "..."                                            │
│     }                                                            │
│                                                                  │
│     → Воркер сохраняет автономность                             │
│     → Контейнер остаётся RUNNING                                │
│     → Заказчик получает update, но не должен отвечать           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Принцип

- **JSON = потерял автономность** (по любой причине: done, blocked, failed)
- **API = сохраняю автономность** (просто информирую о прогрессе)

`blocked` и `complete` — оба означают "сделал всё что мог сам, мяч на стороне заказчика".

---

## Lifecycle Контейнера

```
          CREATE (from pool or fresh)
             │
             ▼
    ┌─────────────────┐
    │     RUNNING     │ ◄──────────────────────┐
    │   (работает)    │                        │
    └────────┬────────┘                        │
             │                                 │
             ├── JSON output ──────┐           │
             │   (любой status)    │           │
             │                     ▼           │
             │            ┌─────────────────┐  │
             │            │     PAUSED      │  │
             │            │   (ждёт)        │──┘ new task / answer
             │            └────────┬────────┘
             │                     │
             │                     │ pause_timeout (30 min)
             │                     ▼
             │            ┌─────────────────┐
             └───────────▶│   TERMINATED    │
               timeout    │   (cleanup)     │
              or error    └─────────────────┘
```

### Docker pause/unpause

```python
# Когда воркер вернул JSON (остановился)
await docker.pause(container_id)

# Когда приходит новая задача или ответ
await docker.unpause(container_id)
await send_task(container_id, new_task)
```

**Характеристики PAUSED:**
- CPU = 0 (процессы заморожены)
- Память сохраняется
- Resume < 1 сек
- Claude session (`--resume`) работает

---

## Session Management

### Session = f(project, token_usage, time)

```
┌─────────────────────────────────────────────────────────────────┐
│                    SESSION LIFECYCLE                             │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Сессия привязана к PROJECT, не к задаче                        │
│                                                                  │
│  CONTINUE session если ВСЕ условия:                             │
│    ✓ Тот же project_id                                          │
│    ✓ context_tokens < 150k (из 200k лимита)                     │
│    ✓ last_activity < 24h                                        │
│                                                                  │
│  NEW session если ЛЮБОЕ:                                         │
│    ✗ Другой project_id                                          │
│    ✗ context_tokens ≥ 150k (overflow)                           │
│    ✗ last_activity ≥ 24h (stale)                                │
│    ✗ Явный запрос force_new                                     │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Token Tracking

Claude Code JSON output содержит usage data:

```json
{
  "type": "result",
  "session_id": "...",
  "total_cost_usd": 0.02348695,
  "usage": {
    "input_tokens": 25000,
    "output_tokens": 8000,
    "cache_read_input_tokens": 15000,
    "cache_creation_input_tokens": 5000
  }
}
```

Считаем контекст:
```python
context_tokens = usage["input_tokens"] + usage.get("cache_read_input_tokens", 0)

if context_tokens > 150_000:
    # Сессия переполнена — нужна новая
    await summarize_and_reset_session()
```

### При смене сессии — Project Context

```
┌─────────────────────────────────────────────────────────────────┐
│  OLD SESSION (overflow/stale)                                    │
│       │                                                          │
│       ▼ summarize (LLM или structured extraction)               │
│  ┌─────────────────────────────────────┐                        │
│  │ PROJECT CONTEXT (в БД):             │                        │
│  │ - Architecture: FastAPI + Redis     │                        │
│  │ - Decisions: aiohttp, 5min TTL      │                        │
│  │ - Known issues: date parsing bug    │                        │
│  │ - File structure: src/main.py, etc  │                        │
│  └─────────────────────────────────────┘                        │
│       │                                                          │
│       ▼ inject as system context                                 │
│  NEW SESSION                                                     │
│  "Continuing work on project X. Context: {summary}"             │
└─────────────────────────────────────────────────────────────────┘
```

---

## План Итераций

### MVP (Phase 1.1)

**Цель**: Система не виснет, контейнеры управляются, сессии переиспользуются.

#### Шаг 1: Модель коммуникации

**Задачи:**
- [ ] Определить JSON schema для worker output:
  ```python
  class WorkerOutput(BaseModel):
      status: Literal["complete", "blocked", "failed"]
      result: dict | None = None
      question: str | None = None  # если blocked
  ```
- [ ] Добавить endpoint `POST /api/worker/progress` для async updates
- [ ] Обновить workers-spawner для парсинга обоих типов output

**Definition of Done:**
- Воркер может вернуть structured JSON
- Progress updates доходят до заказчика
- Telegram bot показывает progress (если запрошен)

---

#### Шаг 2: Container Pause/Unpause

**Задачи:**
- [ ] Добавить `pause_container()` и `unpause_container()` в ContainerService
- [ ] После JSON output → автоматически pause
- [ ] При новой задаче для того же воркера → unpause
- [ ] Добавить `pause_timeout` config (default: 30 min)
- [ ] Scheduler job: terminate paused containers по timeout

**Definition of Done:**
- Контейнер в PAUSED потребляет 0 CPU
- Unpause < 1 сек
- Cleanup работает автоматически

---

#### Шаг 3: Token Tracking

**Задачи:**
- [ ] Парсить `usage` из Claude Code JSON output
- [ ] Хранить `context_tokens` per session в Redis/DB
- [ ] Добавить threshold check (150k) перед continue session
- [ ] Logging token usage для мониторинга

**Definition of Done:**
- Система знает сколько токенов в каждой сессии
- При overflow — автоматически new session (без summarization в MVP)

---

#### Шаг 4: Cleanup & Graceful Shutdown

**Задачи:**
- [ ] Hook на docker compose down → terminate all agent containers
- [ ] Hook на workers-spawner shutdown → cleanup
- [ ] Periodic job: find orphaned containers → terminate
- [ ] Health check: if container unresponsive → terminate

**Definition of Done:**
- `docker compose down` не оставляет висящих контейнеров
- Orphaned containers удаляются автоматически

---

### Post-MVP (Phase 2+)

#### Container Pool

```
┌────────────────────────────────────────────────────┐
│              Container Pool Manager                 │
├────────────────────────────────────────────────────┤
│                                                     │
│  WARM (ready):      [C1] [C2] [C3]                 │
│  ACTIVE (working):  [C4] [C5]                      │
│  PAUSED (idle):     [C6] [C7]                      │
│                                                     │
│  Policy:                                            │
│  ├─ min_warm: 3         (instant allocation)       │
│  ├─ max_total: 20       (resource cap)             │
│  ├─ idle_timeout: 5min  → pause                    │
│  ├─ pause_timeout: 30min → terminate               │
│  └─ recycle_after: 10 tasks (fresh state)         │
│                                                     │
└────────────────────────────────────────────────────┘
```

**Задачи:**
- [ ] PoolManager service
- [ ] Warm pool maintenance (background job)
- [ ] Metrics: pool utilization, wait time

---

#### Session Summarization

**Задачи:**
- [ ] LLM-based summarization при session overflow
- [ ] Structured project context storage в БД
- [ ] Auto-inject context в новые сессии
- [ ] Manual "reset context" command для воркера

---

#### Advanced Monitoring

**Задачи:**
- [ ] Dashboard: active workers, token usage, costs
- [ ] Alerts: container leaks, high token usage
- [ ] Per-project cost tracking

---

## Зависимости

| Компонент | Зависит от |
|-----------|------------|
| Container pause | Docker API |
| Token tracking | Claude Code JSON output parsing |
| Session management | Redis (session state) |
| Cleanup | Scheduler service |

---

## Метрики Успеха

### MVP
- [ ] Система работает с 20+ воркерами без degradation
- [ ] `docker compose down` — 0 orphaned containers
- [ ] Paused containers: CPU = 0%
- [ ] Session reuse работает (--resume)

### Post-MVP
- [ ] Container allocation < 2 сек (from warm pool)
- [ ] Token usage visible per project
- [ ] Automatic session summarization работает

---

## Открытые Вопросы

1. **Один воркер = один проект?**
   - MVP: Да, для простоты
   - Post-MVP: Можно переиспользовать с context switch

2. **Factory.ai token tracking?**
   - Нужно проверить структуру их JSON output
   - Fallback: не track, always new session

3. **Graceful shutdown воркера?**
   - Дать сигнал "заверши текущее и остановись"?
   - Или hard terminate?

---

## Связанные Документы

- [orchestrator-cli-pydantic.md](./orchestrator-cli-pydantic.md) — CLI interface
- [github-worker-integration.md](./github-worker-integration.md) — GitHub capability
- [ARCHITECTURE.md](../ARCHITECTURE.md) — общая архитектура
