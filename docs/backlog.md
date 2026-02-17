# Backlog

> **Актуально на**: 2026-02-16

## Active Design & Implementation Plans

| Feature | Plan | Status |
|---------|------|--------|
| **Worker Lifecycle** | [worker-lifecycle.md](./tasks/worker-lifecycle.md) | Planning (нужна переработка под worker-manager) |
| **Secrets Vault** | [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md) | Superseded by [deploy-architecture.md](./plans/deploy-architecture.md) Iter 1 (Fernet) |

---

## 🔴 HIGH Priority

### TesterNode: Ручное тестирование продукта агентом
**Статус**: TODO (заглушка удалена из субграфа, нода будет добавлена после деплоя)

Тестер-нода должна стоять **после деплоя** (не между developer и done как раньше).
Два варианта размещения:

**Вариант A — Staging:**
1. CI собирает Docker-образы (ci.yml)
2. Тестер разворачивает образы на тестовом стенде с тестовыми env-переменными
3. Тестер вручную тыкает все кнопки, проверяет поведение
4. Если ок — разрешает деплой на прод

**Вариант B — Prod (проще для MVP):**
1. Деплой на прод происходит как сейчас (автоматически после CI)
2. Тестер подключается к живому проду со строгими инструкциями (что можно тыкать, что нельзя)
3. Проводит ручное тестирование

**Задачи (общие для обоих вариантов):**
1. Определить откуда тестер берёт тестовые данные / credentials
2. Реализовать ноду как Claude Code worker с доступом к браузеру или API
3. Добавить ноду в субграф после deploy
4. Определить что делать при fail (откат? уведомление? retry?)

---

### CI Monitor Node: Проверка и триаж CI failures
**Статус**: TODO

Отдельная нода между developer и deploy. Мониторит GitHub Actions по commit_sha.
Сейчас эту роль выполняет `_wait_for_ci_and_fix` в `engineering_worker.py` — в будущем вынести в ноду субграфа.

**Текущее поведение** (`_wait_for_ci_and_fix`):
- Поллит GitHub Actions status checks по коммиту
- При fail — отправляет обратно девелоперу

**Целевое поведение (нода):**
1. Мониторит CI workflows (тесты + сборка образов) по commit_sha
2. При fail — инвестигейшн: анализирует логи CI, определяет причину
3. Триаж: делегирует исправление нужному агенту:
   - Ошибки кода / тесты → обратно developer
   - Ошибки инфраструктуры (Dockerfile, compose, CI config) → devops
   - Неустранимые ошибки → пометить как fail, уведомить PO
4. При success — передаёт управление дальше (deploy или tester)

---

### API Authentication
**Статус**: TODO

API endpoints не защищены (только x-telegram-id header).
Любой с доступом к сети может вызывать API.

**Решение**: API key / JWT аутентификация.

---

### Docker Events Listener: Обновление статуса воркеров
**Статус**: TODO

`DockerEventsListener` (worker-manager/src/events.py) — заглушка, не слушает Docker-события.
Когда контейнер воркера умирает (kill, crash, restart), `worker:status:{id}` в Redis остаётся `RUNNING`.
Telegram-бот видит `RUNNING` → шлёт сообщения в стрим мёртвого контейнера → таймаут, пользователь не получает ответ.

**Задачи:**
1. Реализовать подписку на Docker events (`container die/stop/destroy`) через Docker SDK или API
2. При `die`/`stop` — обновлять `worker:status:{id}` → `STOPPED` в Redis
3. Опционально: уведомлять пользователя через callback stream что воркер упал

**Связано с**: Worker Lifecycle (pause/unpause, cleanup)

---

### Resource Limits (Worker Manager)
**Статус**: TODO

Нет ограничений на ресурсы:
- `MAX_CONCURRENT_WORKERS` — количество одновременных контейнеров
- Memory/CPU limits на контейнеры
- Disk usage limits

**Влияние**: Один пользователь может исчерпать все ресурсы хоста.

---

### Admin UI
**Статус**: TODO

Без админки невозможно нормально отлаживать проект. Нужна хотя бы базовая версия.

**Базовая версия:**
- Просмотр: Projects, Workers, Logs
- Мониторинг состояния системы

**Полная версия (позже):**
- Конфигурация через UI (Prompts, Agent selection, TTL)
- Мониторинг (Grafana, Prometheus)

---

## 🟡 MEDIUM Priority

### Redis Streams: унификация consumer'ов и PEL recovery
**Статус**: TODO

**Проблема:**
`RedisStreamClient` в `shared/redis/client.py` уже имеет полноценный `consume()` async iterator с NOGROUP recovery, auto-ACK и connection management. Но из 7 consumer'ов в системе его использует **один** (`worker-wrapper`). Остальные 6 реализуют свой while-loop с xreadgroup/xack, каждый со своими багами и особенностями.

**Текущее состояние (9 мест создают raw redis подключение мимо RedisStreamClient):**

| Consumer | Файл | Паттерн | NOGROUP recovery |
|----------|------|---------|-----------------|
| `_base.py` (eng/deploy workers) | `services/langgraph/src/workers/_base.py` | ACK-on-success | да (через `ensure_consumer_groups`) |
| PO consumer | `services/langgraph/src/po/consumer.py` | concurrent + manual ACK + semaphore + per-user lock | да (добавлено 2026-02-16) |
| Telegram notifications | `services/telegram_bot/src/notifications.py` | auto-ACK | да |
| Scheduler | `services/scheduler/src/main.py` | ACK-on-success | да |
| Scaffolder | `services/scaffolder/src/main.py` | свой цикл | да |
| Worker-manager | `services/worker-manager/src/consumer.py` | свой класс + ensure_group() | да |
| Worker spawner | `services/langgraph/src/clients/worker_spawner.py` | request/response, id="$" | да |

**Ключевые несоответствия:**
- 3 разных паттерна ACK: auto, on-success, concurrent manual
- `id="0"` vs `id="$"` при создании групп
- Часть использует `ensure_all_groups()`, часть — inline `xgroup_create()`
- `decode_responses=True` vs `False` vs не указан
- **Ни один consumer не делает PEL recovery** — сообщения, не получившие ACK (crash, transient error), навсегда зависают в Pending Entries List

**Что нужно сделать:**

1. **Добавить PEL recovery в `consume()`** (~5 строк):
   При старте читать pending сообщения (`id="0"`) перед переключением на новые (`id=">"`). Это гарантирует повторную обработку задач, упавших из-за transient errors.

2. **Добавить manual ACK mode** в `consume()`:
   Текущий `consume()` всегда делает auto-ACK после yield. Для workers нужен ACK-on-success. Варианты:
   - Параметр `auto_ack=True/False` + метод `StreamMessage.ack()`
   - Отдельный `consume_manual()` метод
   - Callback-based API вместо iterator: `consumer.run(handler=process_fn)`

3. **Мигрировать consumer'ов** на `RedisStreamClient.consume()`:
   - Простые (scheduler, notifications): прямая замена
   - Средние (_base.py, scaffolder, worker-manager): адаптация ACK-стратегии
   - Сложные (PO consumer): нужен batch-read + dispatch паттерн, не iterator

4. **Унифицировать publish** — заменить прямые `xadd` вызовы (webhooks, reminders, redis_publisher) на `RedisStreamClient.publish()`.

**Варианты решения:**

**Вариант A — Эволюционный (рекомендуемый):**
Расширить существующий `RedisStreamClient`:
- Добавить `auto_ack` параметр и `StreamMessage.ack()`
- Добавить PEL recovery
- Мигрировать consumer'ов по одному, начиная с простых
- PO consumer — последний, возможно останется со своим паттерном

**Вариант B — Новая абстракция `StreamConsumer`:**
Отдельный класс поверх `RedisStreamClient`:
```python
consumer = StreamConsumer(
    stream="engineering:queue",
    group="capability-workers",
    handler=process_fn,       # async callable
    ack_strategy="on_success", # auto | on_success | manual
    concurrency=1,             # >1 для PO-like паттерна
)
await consumer.run()
```
Больше API surface, но чище для сложных кейсов.

**Оценка**: 2-3 дня. 7 файлов consumer'ов + shared/redis/client.py + тесты.

**Обнаружено при**: E2E тестировании deploy-architecture (2026-02-16). PO consumer падал без recovery при потере Redis streams.

---

### Worker Lifecycle (Pause/Unpause, Cleanup, Token Tracking)
**Статус**: TODO — план в [worker-lifecycle.md](./tasks/worker-lifecycle.md), требует переработки

**Задачи:**
1. Idle pause/wakeup — `docker pause/unpause` по таймауту неактивности
2. Container cleanup при shutdown
3. Token tracking из Claude Code JSON output
4. Creation queue — очередь создания воркеров с приоритетами

---

### E2E тесты
**Статус**: Фазы 1-4 готовы, фазы 5-7 не реализованы

Завершить E2E покрытие. Full system docker-compose validation.

---

### Secrets Encryption
**Статус**: Done (Iteration 1 of deploy-architecture)

- Fernet encryption at rest в `project.config.secrets` (`shared/crypto.py`)
- Graceful degradation для legacy plaintext значений
- Encrypt-on-write миграция
- Старый план [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md) superseded

---

### Caddy Reverse Proxy
**Статус**: TODO

Убрать port management, использовать Caddy для routing по доменам.

---

## 🟢 LOW Priority

### Telegram Bot Pool
Пул pre-registered ботов для автоматического выделения проектам.

### Docker Python SDK
Миграция worker-manager с subprocess на Python Docker SDK.

### Rollback Capability
Откат к предыдущему деплою при failed health checks.

### Cost Tracking
Логирование tokens per request, агрегация по проектам.

### Human Escalation
Эскалация к человеку при застревании агента.

---

## 📦 Phase 3 (Future)

### Agent Architecture
- Engineering Lead (координация)
- Agent-to-agent communication
