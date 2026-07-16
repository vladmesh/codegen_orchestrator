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
| **GitHub Workflow** (deploy.yml) | 10 min | Fail Task (DeployerNode `wait_for_workflow_completion` timeout) |
| **Ansible Provisioning** | 15 min | Kill Process, Fail Task |
| **Developer Worker Task** | 30 min | Kill Container, Fail Task (or Retry if supported) |

---

## 4. Propagation Flow

Как ошибки "всплывают" от низкоуровневых компонентов к пользователю.

### A. CLI / API Errors
1. **Validation Error (Permanent):** Вернуть HTTP 400 + JSON Error. CLI показывает читаемую ошибку.
2. **Infrastructure Error (Transient):** CLI делает ретрай (до 3 раз). Если не вышло — показать "System unavailable, try again later".

### B. Worker Errors (Async)
1. **Crash/OOM:** `worker-manager` ловит exit code != 0 (через Docker events).
   - Публикует результат в output queue: `status="failed", error="Process crashed"`.
2. **Logic Error (in container):** Агент ловит exception.
   - Публикует результат: `status="failed", error="Exception message"`.

### C. Consumer Errors (Redis)

All consumers use unified `RedisStreamClient.consume()` API with two ACK modes:

**Manual ACK (`auto_ack=False`)** — используется большинством consumer'ов:
1. Сообщение читается, но не ACK'ается автоматически.
2. Consumer обрабатывает сообщение.
3. При успехе — `await client.ack(stream, group, msg.message_id)`.
4. При ошибке — ACK не вызывается, сообщение остаётся в PEL.

**Auto ACK (`auto_ack=True`)** — для fire-and-forget (ProactiveListener, ProvisionerNotifier):
1. Сообщение ACK'ается сразу при чтении.
2. Потеря при краше допустима (уведомления, не критичные данные).

**PEL Recovery** (`claim_pending=True`):
- При старте consumer вызывает `XAUTOCLAIM` и подбирает сообщения, зависшие в PEL дольше `pending_timeout_ms` (default: 60s).
- Это покрывает сценарий краша consumer'а mid-processing — после рестарта сообщение автоматически переобрабатывается.
- PEL recovery идёт до основного `XREADGROUP` цикла.

**Error handling flow:**
1. **Processing Error (Transient):** Не вызываем ACK → сообщение остаётся в PEL → PEL recovery подхватит при рестарте.
2. **Processing Error (Permanent):** ACK + XADD в DLQ (если реализован).
3. **Consumer Crash:** Сообщение в PEL → другой инстанс или рестарт подхватит через `XAUTOCLAIM`.

2. **DLQ Handling:** Отдельный процесс или админ ручками разбирает DLQ.

---

## 5. Deploy Error Handling

### Deploy Retry Limit
Deploy worker writes a typed `DeployOutcome` to `run.result`. Environment-contract failures keep
their specific outcome; unclassified subgraph and smoke failures produce `RETRY`. The supervisor
(`supervise_deploying_stories()` in scheduler) reads the outcome and routes accordingly. After
**3 consecutive RETRY outcomes**, the supervisor transitions the story to `failed`. This prevents
the infinite deploy→fail→redispatch loop.

### Deploy→Engineering Feedback Loop
The supervisor still accepts legacy `CODE_FIX` and `SMOKE_FAILURE` outcomes by creating a fix task
and dispatching it to `engineering:queue`. The current deploy worker does not infer those outcomes:
unknown failures use the bounded `RETRY` path. A future remediation agent may diagnose failed runs
asynchronously and propose a tested code fix outside the deploy path.

### Deploy Deduplication
Atomic `SET NX` Redis lock per project prevents duplicate deploys. Replaces the non-atomic DB-based check that had a race window. Lock held for duration of deploy, released in `finally` block.

### Stale Worker Cleanup
`_check_project_lock()` in the engineering consumer verifies `worker:status` in Redis. Workers in terminal states (`DEAD`/`FAILED`/`STOPPED`) get their Redis keys cleaned up automatically, unblocking new task dispatch without manual intervention.

### Proactive Message Spam Filter
Only two events reach the user via `po:proactive`: (1) deploy success, (2) permanent story failure. All intermediate failures (smoke, precheck, workflow) are handled internally without spamming the user.

---

## 6. Dead Letter Queue (DLQ)

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
