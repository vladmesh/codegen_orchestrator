# Brainstorm: Deploy Failure LLM Classifier — CODE vs INFRA Triage

> **Дата**: 2026-03-16
> **Контекст**: Deploy-worker слепо отправляет все failure в engineering queue, даже если проблема инфраструктурная
> **Status**: done

---

## Current State

Deploy consumer (`services/langgraph/src/consumers/deploy.py`) при failure вызывает `_redispatch_to_engineering()` в двух местах:

1. **Smoke failure** (line 209): контейнеры поднялись, но healthcheck/smoke упал → dispatch to engineering + story → "start"
2. **Devops subgraph errors** (line 666): workflow failed, нет deployed_url → dispatch to engineering + `_handle_deploy_failure()` (retry counter → fail после 3)

В обоих случаях engineering worker получает задачу "fix the code so containers start cleanly" — даже если проблема в timeout, сети или ресурсах сервера.

## Problem

Инфра-проблемы (healthcheck timeout, медленная миграция, нехватка RAM, сетевые проблемы) неотличимы от code-проблем (import error, crash, неправильный config) без анализа error details. Engineering worker крутится 7+ минут без пользы, тратит tokens и время.

## Решение

Добавить LLM-классификатор перед `_redispatch_to_engineering()`. Один вызов haiku с промптом:

```
Classify this deployment failure as CODE or INFRA.

CODE = application bug (import error, crash, missing dependency, wrong config key, syntax error)
INFRA = infrastructure issue (timeout, healthcheck slow start, network, resource limits, SSH)

Error details:
{error_details}

Reply with exactly one word: CODE or INFRA
```

### Логика ветвления

```
deploy failure
  → LLM classify(error_details)
    → CODE: _redispatch_to_engineering() (как сейчас, до 2 попыток)
    → INFRA: skip engineering, retry deploy
              → retry тоже failed → story="failed" (HITL)
```

### Где менять

Одна новая функция `_classify_deploy_failure()` в `deploy.py`. Вызывается из двух мест:

**Место 1 — `_handle_smoke_failure()` (line 208-213):**
```python
# Before:
await _redispatch_to_engineering(redis=redis, msg=msg, error_details=smoke_details)

# After:
failure_type = await _classify_deploy_failure(smoke_details)
if failure_type == "CODE":
    await _redispatch_to_engineering(redis=redis, msg=msg, error_details=smoke_details)
else:
    logger.info("deploy_failure_classified_infra", task_id=msg.task_id)
    # Story already transitions to "start" → dispatcher will re-deploy
    # Retry counting handled by _handle_deploy_failure path
```

**Место 2 — main flow (line 665-670):**
```python
# Before:
await _redispatch_to_engineering(redis=redis, msg=msg, error_details=error_msg)

# After:
failure_type = await _classify_deploy_failure(error_msg)
if failure_type == "CODE":
    await _redispatch_to_engineering(redis=redis, msg=msg, error_details=error_msg)
else:
    logger.info("deploy_failure_classified_infra", task_id=task_id)
    # _handle_deploy_failure() below handles retry counter + story="failed" after limit
```

### Retry/fail для INFRA

Место 2 уже вызывает `_handle_deploy_failure()` после dispatch — там есть retry counter (`deploy:{story_id}:attempts`, max=3, потом story="failed"). Если мы просто пропускаем engineering dispatch, retry логика работает as-is.

Место 1 (`_handle_smoke_failure`) НЕ использует retry counter — нужно добавить. Самое простое: вызвать `_handle_deploy_failure()` вместо ручного `_transition_story_safe(story_id, "start")` когда INFRA.

### Детали реализации

- **Модель**: haiku (дёшево, быстро, достаточно для binary classification)
- **Fallback**: если LLM вернул что-то кроме CODE/INFRA или timeout — считаем CODE (safe default, текущее поведение)
- **Логирование**: `deploy_failure_classified`, classification=CODE/INFRA, error_details (truncated)
- **Без нового LLM-клиента**: в deploy consumer уже есть доступ к LLM через langchain (devops subgraph его использует). Можно использовать простой `ChatAnthropic(model="claude-haiku-4-5-20251001").ainvoke()`

### Объём изменений

- 1 файл: `services/langgraph/src/consumers/deploy.py`
- ~40-50 строк новой функции `_classify_deploy_failure()`
- ~10 строк изменений в двух точках вызова
- Тесты: mock LLM response, проверить обе ветки

## Action Items

- → new task: "Add LLM classifier (CODE vs INFRA) to deploy failure handling" — deploy.py, haiku call before `_redispatch_to_engineering()`, INFRA → retry deploy, CODE → engineering as before. INFRA retry exhausted → story=failed (HITL).
