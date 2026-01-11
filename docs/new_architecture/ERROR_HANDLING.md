# Error Handling Strategy

Общая стратегия обработки ошибок для всех сервисов codegen_orchestrator.

---

## 1. Error Categories

Все ошибки классифицируются на 4 категории для принятия решения о retry.

| Category | Description | Examples | Retry Strategy |
|----------|-------------|----------|----------------|
| **TRANSIENT** | Временный сбой, успех вероятен при повторе | Redis timeout, API HTTP 503, Network glitch | **Retry** (Short backoff) |
| **UPSTREAM** | Сбой внешней системы, требующий ожидания | GitHub Rate Limit, OpenAI API Overload | **Retry** (Long backoff) |
| **PERMANENT** | Логическая ошибка, повтор бессмысленен | HTTP 400 Bad Request, Validation Error, 404 | **No Retry** (Fail fast) |
| **FATAL** | Критическая проблема конфигурации | Auth failed, DB Connection failed (persistent), Config missing | **Abort** (Alert admin) |

---

## 2. Retry Policy

Глобальные политики ретраев. Конкретные значения могут переопределяться в конфиге сервиса.

### Transient Policy
- **Count:** 3 retries
- **Backoff:** Exponential (1s, 2s, 4s)
- **Jitter:** ±100ms
- **On Exhaust:** Move to DLQ or Fail Task

### Upstream Policy
- **Count:** 5 retries
- **Backoff:** Exponential (5s, 10s, 20s, 60s...)
- **On Exhaust:** Notify Admin, Fail Task

---

## 3. Timeout Policy

Таймауты для предотвращения зависания системы.

| Operation | Default Timeout | Action on Timeout |
|-----------|-----------------|-------------------|
| **API Request** (Internal) | 10s | Retry (Transient) |
| **Redis Command** (XADD/XREAD) | 5s | Retry (Transient) |
| **Worker Container Spawn** | 60s | Fail (Permanent) |
| **GitHub Workflow** | 30 min | Cancel Workflow, Fail Task |
| **Ansible Provisioning** | 15 min | Kill Process, Fail Task |
| **Developer Worker Task** | 30 min | Kill Container, Fail Task (or Retry if supported) |

---

## 4. Propagation Flow

Как ошибки "всплывают" от низкоуровневых компонентов к пользователю.

### A. CLI / API Errors
1. **Validation Error (Permanent):** Вернуть HTTP 400 + JSON Error. CLI показывает читаемую ошибку.
2. **Infrastructure Error (Transient):** CLI делает ретрай (до 3 раз). Если не вышло — показать "System unavailable, try again later".

### B. Worker Errors (Async)
1. **Crash/OOM:** `worker-manager` ловит exit code != 0.
   - Публикует events в `worker:lifecycle`: `status="failed"`.
   - Публикует результат в output queue: `status="failed", error="Process crashed"`.
2. **Logic Error (in container):** Агент ловит exception.
   - Публикует результат: `status="error", error="Exception message"`.

### C. Consumer Errors (Redis)
1. **Processing Error:**
   - Если ошибка `Transient` — NACK (не подтверждать), пусть Redis передоставит через `retry_time`.
   - Если ошибка `Permanent` — XACK (подтвердить) + XADD в `dead-letter-queue`.
2. **DLQ Handling:** Отдельный процесс или админ ручками разбирает DLQ.

---

## 5. Dead Letter Queue (DLQ)

**Naming Convention:** `{original_queue}:dlq`
- `engineering:queue:dlq`
- `deploy:queue:dlq`

**When to send to DLQ:**
1. Сообщение невалидно (не парсится Pydantic).
2. Исчерпаны ретраи для Transient ошибок.
3. Логическая ошибка, которую сервис не может обработать.

**Payload:**
Копия оригинального сообщения + метаданные ошибки:
```json
{
  "original_message": {...},
  "error_context": {
    "error": "ValueError: Invalid project_id",
    "timestamp": "...",
    "service": "langgraph",
    "attempts": 3
  }
}
```
