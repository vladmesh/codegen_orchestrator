# Custom Memory Management Plan

## Проблема

Текущий `MemorySaver` в LangGraph:
- Хранит все промежуточные состояния графа в RAM
- При рестарте теряется вся история (но это приемлемо)
- Потенциально может переполнить память при долгих сессиях

**Цель**: Контролируемое управление памятью LangGraph + персистенция сообщений Telegram через существующую инфраструктуру.

## Текущее состояние (уже реализовано)

### ✅ Sliding Window в worker.py
```python
# services/langgraph/src/worker.py:32-33
MAX_HISTORY_SIZE = 10
conversation_history: dict[str, list] = defaultdict(list)
```
Последние 10 сообщений хранятся в `conversation_history`, передаются в state при каждом invoke.

### ✅ Сохранение Telegram сообщений
```python
# services/telegram_bot/src/main.py:135-143, 188-196
await _post_rag_message({
    "telegram_id": user_id,
    "role": "user" | "assistant",
    "message_text": text,
    ...
})
```
Оба направления (user → bot, bot → user) уже сохраняются в `rag_messages`.

### ✅ RAGMessage модель + API
- `shared/models/rag.py:RAGMessage` — с полем `summarized_at`
- `POST /api/rag/messages` — эндпоинт для сохранения
- `services/api/src/schemas/rag.py:RAGMessageCreate` — схема

### ✅ Summarizer Worker
```python
# services/scheduler/src/tasks/rag_summarizer.py
# Суммаризирует когда накопилось > RAG_SUMMARY_TOKEN_THRESHOLD токенов
```
- Группирует по `user_id`
- Генерирует summary через LLM
- Сохраняет в `rag_conversation_summaries`
- Помечает сообщения как `summarized_at = now()`

---

## Что нужно доработать

> [!NOTE]
> **MVP Decision**: MemorySaver eviction отложен в backlog. При ~2.7KB на checkpoint и ожидаемой нагрузке (~20MB за неделю) это не критично для MVP.

### Phase 1: Context Enrichment (Priority: MEDIUM)

#### 2.1 Подгружать conversation summaries при старте

**Проблема**: При рестарте worker'а или после очистки MemorySaver история теряется.

**Решение**: Enrichment из `rag_conversation_summaries` перед invoke.

##### [MODIFY] [worker.py](file:///home/vlad/projects/codegen_orchestrator/services/langgraph/src/worker.py)

```python
async def _get_conversation_context(user_id: int) -> str | None:
    """Fetch recent conversation summaries for context enrichment."""
    try:
        response = await api_client.get(
            f"rag/summaries?user_id={user_id}&limit=3"
        )
        summaries = response.json()
        if summaries:
            return "\n\n".join(s["summary_text"] for s in summaries)
    except Exception as e:
        logger.warning("context_enrichment_failed", error=str(e))
    return None

async def process_message(...):
    # ... existing code ...
    
    # Enrich context if history is empty
    if not history and internal_user_id:
        context = await _get_conversation_context(internal_user_id)
        if context:
            history.insert(0, SystemMessage(
                content=f"[Предыдущий контекст диалога]\n{context}"
            ))
```

##### [NEW] API Endpoint для получения summaries

```python
# services/api/src/routers/rag.py
@router.get("/summaries", response_model=list[RAGSummaryRead])
async def get_summaries(
    user_id: int,
    limit: int = 5,
    db: AsyncSession = Depends(get_async_session),
):
    """Get recent conversation summaries for a user."""
    query = (
        select(RAGConversationSummary)
        .where(RAGConversationSummary.user_id == user_id)
        .order_by(RAGConversationSummary.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(query)
    return list(result.scalars().all())
```

---

### Phase 3: Monitoring (Priority: LOW)

#### 3.1 Метрики использования памяти

```python
# Добавить логирование размера conversation_history
async def _log_memory_stats():
    total_messages = sum(len(h) for h in conversation_history.values())
    thread_count = len(conversation_history)
    logger.info(
        "memory_stats",
        thread_count=thread_count,
        total_messages=total_messages,
    )
```

---

## Verification Plan

### Automated Tests

```bash
# Unit tests для TTLMemorySaver
cd /home/vlad/projects/codegen_orchestrator
make test PYTEST_ARGS="-k test_ttl_memory_saver -v"

# Integration test для context enrichment
make test PYTEST_ARGS="-k test_context_enrichment -v"
```

### Manual Verification

1. **Memory eviction test**:
   - Запустить stack: `docker compose up -d`
   - Отправить сообщения через Telegram
   - Проверить логи: `docker compose logs langgraph | grep memory_stats`
   - Убедиться что thread_count не растёт бесконечно

2. **Context enrichment test**:
   - Перезапустить langgraph: `docker compose restart langgraph`
   - Отправить сообщение через Telegram
   - Проверить что бот "помнит" контекст из summaries

3. **Summarization test**:
   - Проверить что summaries создаются: 
     ```sql
     SELECT * FROM rag_conversation_summaries ORDER BY created_at DESC LIMIT 5;
     ```

---

## Implementation Roadmap

| Phase | Task | Effort | Priority |
|-------|------|--------|----------|
| 1.1 | GET /rag/summaries endpoint | 1h | MEDIUM |
| 1.2 | Context enrichment in worker | 1h | MEDIUM |
| 2.1 | Memory stats logging | 30min | LOW |

**Total estimated effort**: 2-3 hours

---

## Notes

- **Не храним промежуточные tool calls** — только Telegram сообщения user/assistant
- **Суммаризация по токенам** — уже реализована, порог настраивается через `RAG_SUMMARY_TOKEN_THRESHOLD`
- **При падении графа** — история из `conversation_history` теряется, но восстанавливается из summaries
