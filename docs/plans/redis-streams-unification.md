# Redis Streams: Consumer Unification + Queue Contract Enforcement

> **Backlog**: #3 (PEL Recovery & Consumer Unification) + #5 (Queue Contract Enforcement)
> **Создан**: 2026-02-24
> **Статус**: ✅ Выполнено (с отклонениями, см. замечания)

## Context

8 consumer'ов реализуют свои while-loop и ACK-паттерны. Часть очередей не имеет Pydantic-контрактов (po:input, po:response, po:proactive), другие имеют контракты, но не используют на publish/consume. PEL recovery отсутствует — после краша задачи тихо теряются.

**Цель**: Единый `consume()` API с manual ACK и PEL recovery. Pydantic-контракты на все очереди.

---

## PR 1: RedisStreamClient enhancements + PO contracts (shared layer) — ✅ DONE

> Чистое расширение shared-кода, не затрагивает runtime сервисов.

### 1.1 Расширить `consume()` — manual ACK, PEL recovery, flat-fields — ✅ DONE

**File**: `shared/redis/client.py`

**Новый метод**:
```python
async def ack(self, stream: str, group: str, message_id: str) -> None
```

**Новые параметры `consume()`**:
```python
async def consume(
    self, stream, group, consumer,
    block_ms=5000, count=1,
    auto_ack=True,              # NEW: False = caller must ack()
    claim_pending=True,         # NEW: recover PEL on startup
    pending_timeout_ms=60_000,  # NEW: min idle time before re-claim
) -> AsyncIterator[StreamMessage]:
```

**Flat-field парсинг** — реализован как статический метод `_parse_fields()`.

**PEL recovery** — реализован как `_recover_pending()` async generator, вызывается перед основным while-loop.

> **Риск**: fakeredis может не поддерживать xautoclaim. ✅ Поддерживает, fallback не понадобился.

**Тесты**: `shared/tests/test_redis_client.py` — все добавлены ✅
- `TestAck` — ack() делает xack
- `TestConsumeManualAck` — auto_ack=False не ackает; ack() вручную работает
- `TestConsumePELRecovery` — claim_pending=True подбирает pending перед новыми
- `TestConsumeFlatFields` — flat-field сообщения парсятся корректно

**Дополнительно реализовано** (не было в плане):
- `publish_flat()` метод для публикации flat-field сообщений без JSON "data" обёртки

### 1.2 PO Contracts — ✅ DONE

**New file**: `shared/contracts/queues/po.py` — создан ✅
**Тесты**: `shared/tests/test_po_contracts.py` — созданы ✅
**Re-export**: `shared/contracts/queues/__init__.py` — обновлён ✅

### 1.3 Изменить `EngineeringMessage.user_id: int` -> `str` — ✅ DONE

**File**: `shared/contracts/queues/engineering.py` — уже `str`, изменение не требовалось.

---

## PR 2: Миграция consumer'ов на unified consume() — ✅ DONE (с замечаниями)

> Поочерёдная миграция от простых к сложным.

### 2.1 Scheduler — provisioner_results_worker — ✅ DONE
**File**: `services/scheduler/src/main.py`
- `consume(auto_ack=False, claim_pending=True)` ✅
- `ProvisionerResult.model_validate()` ✅

### 2.2 Telegram Bot — ProvisionerNotifier — ✅ DONE
**File**: `services/telegram_bot/src/notifications.py`
- `consume(auto_ack=True)` ✅
- `ProvisionerResult.model_validate()` ✅

### 2.3 Telegram Bot — ProactiveListener — ✅ DONE (с замечанием)
**File**: `services/telegram_bot/src/main.py` (class `ProactiveListener`)
- `RedisStreamClient` + `consume(auto_ack=True)` ✅

> ⚠️ **Замечание**: `POProactiveMessage` контракт на consume **не добавлен** — данные читаются напрямую из `msg.data["user_id"]` / `msg.data["text"]`. Риск низкий (auto_ack=True, fire-and-forget), но отсутствует валидация входящих данных.

### 2.4 Scaffolder — ✅ DONE
**File**: `services/scaffolder/src/main.py`
- `consume(auto_ack=False, claim_pending=True)` ✅
- `ScaffolderMessage.model_validate()` ✅

### 2.5 Infra Service — ✅ DONE (с замечанием)
**File**: `services/infra-service/src/main.py`
- `consume(auto_ack=False, claim_pending=True)` ✅

> ⚠️ **Замечание**: `ProvisionerMessage.model_validate()` на consume **не добавлен** — данные передаются в `process_provisioner_job()` как raw dict.

### 2.6 Worker Manager — ✅ DONE
**File**: `services/worker-manager/src/consumer.py`
- `consume(auto_ack=False, claim_pending=True)` ✅
- `TypeAdapter(WorkerCommand).validate_python()` ✅
- **Фикс бага**: ACK после обработки ✅

### 2.7 Engineering/Deploy Workers (_base.py) — ✅ DONE (с замечанием)
**File**: `services/langgraph/src/workers/_base.py`
- `consume(auto_ack=False, claim_pending=True)` ✅
- `ack()` после обработки ✅

> ⚠️ **Замечание**: Schema validation в `_base.py` **не добавлена** — raw dict передаётся в `process_fn`. Это осознанное решение: `_base.py` — generic worker, валидация делегируется в конкретную `process_fn`.

### 2.8 PO Consumer (ОСОБЫЙ СЛУЧАЙ) — ✅ DONE
**File**: `services/langgraph/src/po/consumer.py`
- Custom while-loop сохранён (concurrent dispatch) ✅
- PEL recovery на старте через `_recover_pending()` + `xautoclaim` ✅
- `POInputMessage` через TypeAdapter на consume ✅
- `POResponse` / `POProactiveMessage` + `to_flat_fields()` на publish ✅

---

## PR 3: Contract enforcement на стороне publish — ✅ DONE (с замечаниями)

> Все producers используют Pydantic-контракты вместо raw dicts.

### 3.1 `trigger_engineering()` -> EngineeringMessage — ✅ DONE (с замечанием)
**File**: `services/langgraph/src/po/tools.py`
- Использует `EngineeringMessage(...)` ✅

> ⚠️ **Замечание**: Публикация через raw `redis.xadd()` с `model_dump_json()`, а **не** через `client.publish_message()`. Контракт применяется, но вызов не унифицирован.

### 3.2 `publish_callback_event()` -> POSystemEvent — ✅ DONE
**File**: `services/langgraph/src/workers/_events.py`
- `POSystemEvent` + `to_flat_fields()` + `publish_flat()` ✅
- `POProactiveMessage` + `to_flat_fields()` + `publish_flat()` ✅

### 3.3 Telegram bot -> POUserMessage — ✅ DONE (с замечанием)
**File**: `services/telegram_bot/src/main.py`
- `POUserMessage` + `to_flat_fields()` ✅

> ⚠️ **Замечание**: Публикация через raw `redis.xadd()`, а **не** через `client.publish_flat()`. Telegram bot использует raw Redis connection (не RedisStreamClient) для PO-публикации. Контракт применяется, но вызов не унифицирован.

### 3.4 Reminders -> POReminderMessage — ✅ DONE (с замечанием)
**File**: `services/langgraph/src/po/reminders.py`
- `POReminderMessage` + `to_flat_fields()` ✅

> ⚠️ **Замечание**: Публикация через raw `redis.xadd()`, а **не** через `client.publish_flat()`. Reminders poller использует raw Redis connection. Контракт применяется, но вызов не унифицирован.

### 3.5 Provisioner client -> ProvisionerMessage — ✅ DONE
**File**: `services/langgraph/src/clients/provisioner_client.py`
- `ProvisionerMessage(...)` + `client.publish_message()` ✅

### 3.6 Infra service result -> ProvisionerResult — ✅ DONE (с замечанием)
**File**: `services/infra-service/src/main.py`
- `ProvisionerResult` используется ✅

> ⚠️ **Замечание**: Публикация через raw `client.publish()` / `client.redis.set()`, а **не** через `client.publish_message()`. Контракт применяется, но вызов не унифицирован.

---

## Current Contract Status

| Queue | Contract | Publish | Consume | Status |
|-------|----------|---------|---------|--------|
| engineering:queue | `EngineeringMessage` | ✅ model_dump_json (raw xadd) | raw dict → process_fn | ✅ |
| deploy:queue | `DeployMessage` | ✅ model_dump_json | raw dict → process_fn | ✅ |
| scaffolder:queue | `ScaffolderMessage` | ✅ model_dump_json | ✅ model_validate | ✅ |
| provisioner:queue | `ProvisionerMessage` | ✅ publish_message | raw dict | ⚠️ consume без валидации |
| provisioner:results | `ProvisionerResult` | ✅ model (raw publish) | ✅ model_validate | ✅ |
| worker:commands | `WorkerCommand` | ✅ model_dump_json | ✅ validate_python | ✅ |
| po:input | `POInputMessage` | ✅ to_flat_fields (raw xadd) | ✅ TypeAdapter validate | ✅ |
| po:response:* | `POResponse` | ✅ to_flat_fields | raw dict (telegram bot) | ⚠️ consume без валидации |
| po:proactive | `POProactiveMessage` | ✅ to_flat_fields + publish_flat | raw dict (telegram bot) | ⚠️ consume без валидации |

---

## Consumer Inventory (итоговое состояние)

| # | Consumer | File | Queue | ACK | PEL Recovery | Validation |
|---|----------|------|-------|-----|-------------|------------|
| 1 | Engineering Worker | `workers/_base.py` | engineering:queue | ✅ manual | ✅ claim_pending | ⚠️ в process_fn |
| 2 | Deploy Worker | `workers/_base.py` | deploy:queue | ✅ manual | ✅ claim_pending | ⚠️ в process_fn |
| 3 | PO Consumer | `po/consumer.py` | po:input | ✅ finally | ✅ xautoclaim | ✅ TypeAdapter |
| 4 | Worker Manager | `worker-manager/consumer.py` | worker:commands | ✅ manual (fixed) | ✅ claim_pending | ✅ validate_python |
| 5 | Scaffolder | `scaffolder/main.py` | scaffolder:queue | ✅ manual | ✅ claim_pending | ✅ model_validate |
| 6 | Infra Service | `infra-service/main.py` | provisioner:queue | ✅ manual | ✅ claim_pending | ⚠️ raw dict |
| 7 | Scheduler | `scheduler/main.py` | provisioner:results | ✅ manual | ✅ claim_pending | ✅ model_validate |
| 8 | Provisioner Notifier | `telegram_bot/notifications.py` | provisioner:results | ✅ auto | — | ✅ model_validate |
| 9 | Proactive Listener | `telegram_bot/main.py` | po:proactive | ✅ auto | — | ⚠️ raw dict |

---

## Остаточные замечания (backlog)

Ниже — мелкие улучшения, не блокирующие, но повышающие consistency:

1. **ProactiveListener** — добавить `POProactiveMessage` валидацию на consume
2. **Infra Service consumer** — добавить `ProvisionerMessage.model_validate()` на consume
3. **Telegram bot PO publish** — перевести с raw `redis.xadd` на `client.publish_flat()`
4. **Reminders publish** — перевести с raw `redis.xadd` на `client.publish_flat()`
5. **PO tools trigger_engineering** — перевести с raw `redis.xadd` на `client.publish()` (или `publish_message()`)
6. **Infra service result publish** — перевести на `client.publish_message()`

---

## Verification

```bash
make test-unit          # ✅ 490 tests pass
make lint               # ✅ Clean
```

Manual:
1. `make up` — все сервисы стартуют
2. Telegram message -> PO -> response приходит
3. Логи без ошибок десериализации
4. Kill consumer mid-processing -> restart -> PEL recovery подбирает сообщение
