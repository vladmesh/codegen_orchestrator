# Engineering Subgraph Error Handling Fix

> **Приоритет**: High
> **Оценка**: ~1 час
> **Статус**: ✅ Done (2026-01-10)

## Проблема

Engineering subgraph сообщает об успехе даже когда DeveloperNode упал с ошибкой.

### Пример из логов

```
developer_node_exception  error="Client error '404 Not Found'..."
                          error_type=HTTPStatusError

tester_node_run_start     iteration_count=0    # <-- Почему выполняется?!

engineering_job_success   commit_sha=None      # <-- Успех???
```

### Причина

1. DeveloperNode ловит исключение и возвращает `engineering_status: "blocked"`
2. Но граф имеет **безусловный edge** `developer → tester`
3. TesterNode — заглушка, всегда возвращает `engineering_status: "done"`
4. TesterNode **перезаписывает** статус developer'а
5. Engineering worker видит `status == "done"` → SUCCESS

### Текущий flow

```
START → developer → tester → done/blocked → END
            ↓           ↓
        "blocked"    "done" (перезаписывает!)
```

## Решение

Добавить **conditional edge после developer** для проверки статуса.

### Новый flow

```
START → developer ─┬─ (status=blocked) → blocked → END
                   │
                   └─ (status!=blocked) → tester → done/blocked → END
```

### Реализация

#### 1. Добавить routing function в `engineering.py`

```python
def route_after_developer(state: EngineeringState) -> str:
    """Route based on developer node result.

    If developer returned 'blocked' (error/exception), skip tester.
    Otherwise proceed to tester for validation.
    """
    status = state.get("engineering_status", "idle")

    if status == "blocked":
        return "blocked"

    return "tester"
```

#### 2. Обновить граф в `create_engineering_subgraph()`

```python
def create_engineering_subgraph() -> StateGraph:
    graph = StateGraph(EngineeringState)

    # Add nodes
    graph.add_node("developer", developer_node.run)
    graph.add_node("tester", tester_node.run)
    graph.add_node("done", done_node.run)
    graph.add_node("blocked", blocked_node.run)

    # Edges
    graph.add_edge(START, "developer")

    # БЫЛО:
    # graph.add_edge("developer", "tester")  # <-- безусловный

    # СТАЛО:
    graph.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "blocked": "blocked",
            "tester": "tester",
        },
    )

    graph.add_conditional_edges(
        "tester",
        route_after_tester,
        {
            "done": "done",
            "blocked": "blocked",
            "developer": "developer",
        },
    )

    graph.add_edge("done", END)
    graph.add_edge("blocked", END)

    return graph.compile()
```

#### 3. Опционально: TesterNode должен проверять статус

Если tester всё же вызывается, он не должен перезаписывать `blocked`:

```python
class TesterNode(FunctionalNode):
    async def run(self, state: EngineeringState) -> dict:
        # Не перезаписывать blocked статус
        if state.get("engineering_status") == "blocked":
            return {}  # Ничего не менять

        # ... остальная логика
```

## Файлы для изменения

1. `services/langgraph/src/subgraphs/engineering.py`:
   - Добавить `route_after_developer()`
   - Заменить `add_edge("developer", "tester")` на `add_conditional_edges()`
   - Опционально: обновить TesterNode

## Acceptance Criteria

- [ ] Если DeveloperNode возвращает `blocked`, TesterNode не вызывается
- [ ] Engineering task получает статус `blocked` (не `done`)
- [ ] Ошибка пробрасывается в task.error_message
- [ ] PO агент видит реальный статус (fail, а не success)

## Тестирование

```bash
# 1. Создать проект без репо на GitHub
# 2. Запустить engineering trigger
# 3. Проверить логи:
docker compose logs engineering-worker --tail=50

# Ожидаемый результат:
# developer_node_exception ...
# engineering_job_failed ... (НЕ success!)
```

## Связанные задачи

- TesterNode заглушка — отдельная задача (Phase 3)
- Сейчас фиксим только error propagation
