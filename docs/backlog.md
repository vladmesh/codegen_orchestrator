# Backlog

> **Актуально на**: 2026-02-17

## Active Design & Implementation Plans

| Feature | Plan | Status |
|---------|------|--------|
| **Worker Lifecycle** | [worker-lifecycle.md](./tasks/worker-lifecycle.md) | Planning (нужна переработка под worker-manager) |
| **Secrets Vault** | [secrets-vault-implementation.md](./tasks/secrets-vault-implementation.md) | Superseded by [deploy-architecture.md](./plans/deploy-architecture.md) Iter 1 (Fernet) |

---

## 🔴 HIGH Priority

### Simplify service_template (Optional Backend/Postgres)
**Статус**: TODO

**Проблема**: `service_template` сейчас не даёт возможности создавать простых Telegram-ботов без бэкенда и базы данных. Даже для "echo-bot" тянется целый сервис и настоящий PostgreSQL. Это замедляет разработку и потребляет лишние ресурсы на проде.

**Влияние**: Замедление тестов и разработки (особенно на этапе прототипирования новых функций).

**Задачи (Cross-project):**
1. **codegen_orchestrator**: Обновить Scaffolder и логику выбора модулей, чтобы можно было отказаться от backend/db.
2. **service_template**: Доработать шаблон, чтобы backend и postgres были опциональными модулями (conditional inclusion в docker-compose и коде).

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

### Remove obsolete Zavhoz code and mentions
**Статус**: TODO

**Проблема**: В кодовой базе остался LLM-агент `Zavhoz` (например, в `scripts/agent_configs.yaml`), который планировался для управления ресурсами инфраструктуры. Фактически эта функциональность полностью заменена детерминированным `ResourceAllocatorNode` в Engineering-субграфе. Мертвый код и конфиги агента создают значительную путаницу в архитектуре оркестратора.

**Задачи:**
1. Удалить определение `Zavhoz` из `scripts/agent_configs.yaml` и `scripts/cli_agent_configs.yaml`.
2. Вычистить комментарии и упоминания Zavhoz в `services/langgraph/src/tools/__init__.py` и других файлах.
3. Проверить инструменты (`tools/servers.py`, `tools/ports.py`) на предмет завязок на агента, оставить их только для `ResourceAllocatorNode`.
4. Удалить остаточные упоминания в документации (кроме уже очищенного `docs/resource-management.md`).

---

### API Authentication
**Статус**: TODO

API endpoints не защищены (только x-telegram-id header).
Любой с доступом к сети может вызывать API.

**Решение**: API key / JWT аутентификация.

---

### Security Audit: серверный провизионинг и деплой
**Статус**: TODO

**Проблема:**
Текущий pipeline провизионинга и деплоя имеет ряд проблем с безопасностью:

1. **SSH как root** — `infra-service` и `deploy.yml` подключаются к managed-серверам как `root`. Нет выделенного deploy-пользователя с ограниченными правами.
2. **Провизионинг без hardening** — managed-серверы (`vps-267179`, `vps-267180`) получают Docker и SSH-ключ, но не проходят через security-роли (`prod_infra/ansible/roles/security`): нет firewall, fail2ban, sshd hardening.
3. **Нет cleanup** — после удаления проекта контейнеры и образы остаются на серверах до ручной очистки (обнаружено: `reverse-bot` работал 37+ часов после завершения E2E тестов).
4. **Секреты в GitHub Actions** — `deploy.yml` получает SSH-ключ и хост как repository secrets. При компрометации репозитория — полный доступ к серверу как root.

**Задачи:**

**Провизионинг (cross-project с `prod_infra`):**
1. Создавать `deploy` пользователя на managed-серверах (как на основном VPS)
2. Применять security-роль: firewall (ufw), fail2ban, sshd hardening, отключение root login
3. Ограничить `deploy` пользователя: только docker compose up/down + доступ к `/opt/apps/`

**Деплой (`service_template` + `codegen_orchestrator`):**
4. `deploy.yml` — подключаться как `deploy`, не `root`
5. Добавить cleanup: `docker image prune` после каждого деплоя
6. Рассмотреть ротацию SSH-ключей (per-deployment ephemeral keys vs long-lived)

**Мониторинг:**
7. Периодическая проверка: нет ли "забытых" контейнеров от удалённых проектов (scheduler job)

**Обнаружено при**: ручной чистке серверов после E2E тестов (2026-02-18).

---

### Workspace Failure Counter + Force Clean + Retry Limit
**Статус**: TODO (workspace-persistence фаза 6, отложена)

**Контекст**: Фазы 1-5 workspace persistence реализованы. Workspace сохраняется между попытками, агент продолжает через PROGRESS.md. Но если workspace в сломанном состоянии (битый git state, конфликтующие файлы, испорченный код), resume будет бесконечно проваливаться. Нужен механизм: N попыток resume → wipe workspace → начать заново → после M попыток сдаться.

**Какие случаи рассматриваем:**
1. **Transient failure** — агент упал по таймауту, OOM, или сетевая ошибка. Workspace валидный, resume поможет.
2. **Broken workspace** — агент сам сломал код до некомпилируемого состояния, git в detached HEAD, merge conflict. Resume бесполезен — нужен wipe.
3. **Невыполнимая задача** — спецификация противоречива, зависимости несовместимы. Wipe тоже не поможет — нужно сдаться и эскалировать.

**Запланированная логика:**
- Попытка 1 (count=0): fresh workspace, clone
- Попытка 2 (count=1): resume (workspace сохранён)
- Попытка 3 (count=2): force wipe → fresh clone
- Попытка 4+ (count>=3): reject spawn, эскалация к PO

**Открытые вопросы — кто знает статус воркера?**

Worker-manager не знает, успешно ли завершился воркер. `delete_worker()` убивает контейнер, а `worker:status` отражает lifecycle (RUNNING/STOPPED), не бизнес-результат. Реальный результат (PR создан / задача провалена) знает engineering-worker в langgraph.

Подходы:

**A. Exit code контейнера** — worker-manager читает exit code через `docker inspect` перед удалением. Exit 0 = success, non-zero = failure. Просто, но грубо: exit code не отличает "задача не решена" от "контейнер убит по OOM".

**B. Параметр в `delete_worker()`** — caller (Docker events listener или consumer) передаёт `exit_status: str`. Требует, чтобы caller знал результат, а сейчас delete вызывается из разных мест.

**C. Счётчик в engineering-worker (langgraph)** — engineering-worker знает результат (PR создан? CI прошёл?). Он ведёт счётчик в Redis и решает: retry с тем же project_id, retry с force_clean, или сдаться. Worker-manager остаётся stateless. Самый чистый по разделению ответственности, но логика retry размазана между двумя сервисами.

**D. Отдельный Redis key от worker-wrapper** — worker-wrapper (entrypoint контейнера) пишет `workspace:{project_id}:last_result = success|failure` перед exit. Worker-manager читает при следующем create. Простая интеграция, не требует изменений в langgraph.

**Рекомендация**: Начать с D (worker-wrapper пишет результат) + логика счётчика в manager. Если окажется недостаточно — мигрировать на C.

**План (из workspace-persistence.md, фаза 6)**: [workspace-persistence.md](./plans/workspace-persistence.md#фаза-6-failure-counter--force-clean--retry-limit)

---

### Worker Reuse for CI Fix Loop
**Статус**: TODO

**Проблема**: При CI failure спаунится новый worker для фикса — clone, контекст потерян, ~30с на создание контейнера. Оригинальный worker, который писал код, уже имеет полный контекст проекта, но убивается сразу после push.

**Целевое поведение**: Не убивать worker до завершения CI pipeline. Если CI упал — отправить fix-задачу в тот же контейнер (тот же Claude Code сессия, тот же контекст).

**Что нужно менять**:
1. **worker-wrapper** — поддержка follow-up задач (сейчас: одна задача → exit)
2. **worker-manager** — не убивать контейнер после завершения первой задачи
3. **engineering_worker** — вместо `request_spawn()` отправлять задачу в существующий контейнер

**Обнаружено при**: анализе BUG 15 (E2E iter 11) — бессмысленные ретраи с новыми worker'ами.

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

### Queue Contract Enforcement
**Статус**: TODO

**Проблема:**
Queue messages между сервисами передаются как raw dict/json без валидации. Pydantic-контракты (`shared/contracts/queues/`) существуют, но не все producers/consumers их используют. Это приводит к рассогласованию полей, тихим ошибкам парсинга и дублям (BUG 7).

**Целевое состояние:**
- Все producers используют `msg.model_dump_json()` при публикации
- Все consumers используют `Model.model_validate(job_data)` при чтении
- Единый формат `{"data": json_string}` для всех очередей

**Прогресс:**
- [x] `deploy:queue` — DeployMessage (этот PR)
- [ ] `engineering:queue` — EngineeringMessage
- [ ] `scaffolder:queue` — ScaffolderMessage (уже используется)
- [ ] `provisioner:queue` — ProvisionerMessage
- [ ] `po:input` / `po:proactive` — PO messages

---

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
**Статус**: Done (Iteration 7 of deploy-architecture)

- Caddy (`caddy:2-alpine`) + Docker Registry (`registry:2`) в docker-compose
- Auto-TLS (Let's Encrypt) через `ORCHESTRATOR_HOSTNAME`
- Routes: `/v2/*` → registry (basic auth), `/webhooks/*` → api
- Registry secrets (`REGISTRY_URL/USER/PASSWORD`) устанавливаются scaffolder'ом до первого CI push

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
