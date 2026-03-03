# Worker Network Isolation

> **Backlog**: #22
> **Создан**: 2026-03-03
> **Статус**: Pending

## Context

E2E тест Level C выявил критическую проблему: агент внутри воркера подключается к postgres **оркестратора** вместо postgres проекта. Причина — DNS-коллизия: имя `db` резолвится в БД оркестратора, потому что воркер сидит на той же сети `codegen_internal`.

Текущий workaround (`project-db` alias + `_patch_db_hostname()`) хрупок: агент может вызвать `make migrate` до патча `.env`, или "починить" `project-db` обратно на `db`, решив что это баг. E2E это подтвердил.

### Целевое состояние

Новая сеть `codegen_worker`: воркер физически не видит postgres/redis оркестратора. `db` внутри воркера резолвится только в `dev_proj_*` — правильная БД проекта. Workaround удалён. Масштабируется до 5 параллельных воркеров на одном хосте.

### Ключевые файлы

| Файл | Роль |
|------|------|
| `docker-compose.yml` | Определение сетей, подключение сервисов |
| `services/worker-manager/src/config.py` | `INTERNAL_NETWORK` setting |
| `services/worker-manager/src/manager.py:530-535` | Выбор сети при создании воркера |
| `services/worker-manager/src/compose_runner.py:20-43` | `_generate_network_override()` — alias `project-db` |
| `packages/orchestrator-cli/src/orchestrator_cli/commands/dev_env.py:123-138` | `_patch_db_hostname()` |
| `services/worker-manager/tests/unit/test_compose_runner.py` | Тесты на network override |
| `services/worker-manager/tests/unit/test_manager_logic.py` | Тесты на create_worker |

### Сетевая топология: до и после

**До (одна сеть):**
```
codegen_internal
┌──────────────────────────────────┐
│  db (postgres оркестратора)       │ ← worker видит!
│  redis, api, worker-manager      │
│  langgraph, eng-worker, ...      │
│                                  │
│  worker-xxx ─────────────────────┼── dev_proj_xxx (db проекта)
└──────────────────────────────────┘

worker$ nslookup db → 172.19.0.2 (postgres ОРКЕСТРАТОРА) ← НЕПРАВИЛЬНО
```

**После (две сети):**
```
codegen_internal               codegen_worker            dev_proj_{id}
┌──────────────────┐          ┌──────────────────┐      ┌──────────────┐
│  db (postgres)    │          │                  │      │ db (проекта)  │
│  langgraph        │          │                  │      │ redis (проекта)│
│  eng-worker       │          │                  │      └───────┬───────┘
│  deploy-worker    │          │                  │              │
│  scheduler        │          │                  │              │
│  caddy, registry  │          │                  │              │
│  telegram_bot     │          │                  │              │
│  infra-service    │          │                  │              │
│                   │          │                  │              │
│  redis ───────────┼──────────┤ redis            │              │
│  api ─────────────┼──────────┤ api              │              │
│  worker-manager ──┼──────────┤ worker-manager   │              │
│                   │          │  worker ──────────┼──────────────┘
└──────────────────┘          └──────────────────┘

worker$ nslookup db → 172.20.0.2 (postgres ПРОЕКТА) ← ПРАВИЛЬНО
```

---

## Iteration 1: Создание сети `codegen_worker` и подключение сервисов

> Добавить новую Docker-сеть. Сервисы redis, api, worker-manager — на обеих сетях. Воркеры переключаются с `codegen_internal` на `codegen_worker`.

### 1.1 docker-compose.yml — новая сеть + dual-homing bridge-сервисов

**Файл**: `docker-compose.yml`

Изменения:
1. В секции `networks:` добавить `worker: name: codegen_worker`
2. Сервисы `redis`, `api`, `worker-manager` — добавить `- worker` в networks (dual-homing)
3. Остальные сервисы (`db`, `langgraph`, `eng-worker`, `deploy-worker`, `scheduler`, `caddy`, `registry`, `telegram_bot`, `infra-service`) — только `internal`, без изменений

### 1.2 config.py — переименовать настройку

**Файл**: `services/worker-manager/src/config.py`

Изменения:
1. Добавить `WORKER_NETWORK: str = "codegen_worker"` — сеть, к которой подключаются воркеры
2. `INTERNAL_NETWORK` оставить (используется для compose env var в docker-compose.yml), но в коде manager.py переключиться на `WORKER_NETWORK`

### 1.3 manager.py — воркеры подключаются к `codegen_worker`

**Файл**: `services/worker-manager/src/manager.py:530-535`

Изменение: заменить `settings.INTERNAL_NETWORK` на `settings.WORKER_NETWORK` в блоке выбора сети.

### 1.4 docker-compose.yml — env var для worker-manager

**Файл**: `docker-compose.yml` (секция worker-manager environment)

Добавить: `WORKER_NETWORK: codegen_worker`

### Критерии приёмки Iteration 1

- [ ] `make up` стартует без ошибок, все сервисы healthy
- [ ] `docker network ls` показывает обе сети: `codegen_internal` и `codegen_worker`
- [ ] `docker network inspect codegen_worker` содержит: redis, api, worker-manager
- [ ] `docker network inspect codegen_internal` НЕ содержит worker-контейнеров
- [ ] `docker network inspect codegen_worker` содержит worker-контейнеры (после создания воркера)
- [ ] Из worker-контейнера: `nslookup db` → resolves только если проект поднял `db` на `dev_proj_*`
- [ ] Из worker-контейнера: `nslookup redis` → resolves (к redis оркестратора через `codegen_worker`)
- [ ] `make test-unit` проходит

---

## Iteration 2: Удаление workaround `project-db`

> Убрать alias `project-db` из network override, убрать `_patch_db_hostname()` из CLI. Воркеру больше не нужен этот хак — `db` резолвится правильно.

### 2.1 compose_runner.py — убрать alias из network override

**Файл**: `services/worker-manager/src/compose_runner.py:20-43`

Изменения в `_generate_network_override()`:
1. Убрать блок `services: db: networks: default: aliases: - project-db`
2. Обновить docstring — убрать упоминание `project-db` и DNS-коллизии
3. Оставить только redirect `default` network → `dev_proj_{worker_id}`

### 2.2 dev_env.py — удалить `_patch_db_hostname()`

**Файл**: `packages/orchestrator-cli/src/orchestrator_cli/commands/dev_env.py`

Изменения:
1. Удалить функцию `_patch_db_hostname()` (строки 123-138)
2. Убрать вызов `_patch_db_hostname()` из `start_infra()` (строка 112)

### 2.3 Обновить тесты

**Файл**: `services/worker-manager/tests/unit/test_compose_runner.py`

Изменения в `test_network_override_generated_for_up`:
- Убрать assert на `project-db` в содержимом override-файла (если есть)
- Проверить что override содержит только network redirect, без services/aliases

### Критерии приёмки Iteration 2

- [ ] `_patch_db_hostname` нигде не вызывается (`grep -r "patch_db_hostname"` → 0 результатов)
- [ ] `project-db` нигде не генерируется (`grep -r "project-db" --include="*.py"` → 0 результатов, только docs)
- [ ] `.codegen-network.yml` содержит только `networks.default` redirect, без `services` секции
- [ ] `make test-unit` проходит
- [ ] `.env` проекта остаётся с `POSTGRES_HOST=db` (нативное значение, без патчинга)

---

## Iteration 3: Тесты и валидация

> Добавить unit-тесты на новую конфигурацию сети. Провести smoke-тест с реальным воркером.

### 3.1 Unit-тест: worker подключается к codegen_worker

**Файл**: `services/worker-manager/tests/unit/test_manager_logic.py`

Новый тест: `test_create_worker_uses_worker_network` — mock settings, проверить что `create_worker` вызывается с `network_name="codegen_worker"` (а не `codegen_internal`).

### 3.2 Unit-тест: network override без project-db alias

**Файл**: `services/worker-manager/tests/unit/test_compose_runner.py`

Обновить `test_network_override_generated_for_up`:
- Assert что override-файл НЕ содержит `project-db`
- Assert что override-файл НЕ содержит секцию `services:`

### 3.3 Smoke-тест с реальным воркером

Ручная проверка (не автоматизируется в unit-тестах):
1. `make up`
2. Создать воркер через engineering pipeline или вручную через API
3. `docker exec` в воркер → `nslookup db` → должен fail (нет проекта)
4. `orchestrator dev-env start-infra db` → `nslookup db` → должен resolve в `dev_proj_*`
5. Подключиться к БД из воркера → должна быть пустая БД проекта, не оркестратора

### Критерии приёмки Iteration 3

- [ ] Новые unit-тесты проходят
- [ ] `make test-unit` проходит целиком
- [ ] Smoke-тест подтверждает: воркер видит только БД проекта, не оркестратора
- [ ] Из воркера невозможно подключиться к `db:5432` оркестратора (connection refused / DNS fail)

---

## Iteration 4: Cleanup документации и brainstorm-файлов

> Привести документацию в порядок. Удалить deep-dive (его полезное содержимое уже учтено).

### 4.1 Обновить brainstorm

**Файл**: `docs/brainstorms/worker-db-isolation.md`

Пометить Phase 1 как выполненную. Оставить Phases 2-4 как reference на будущее.

### 4.2 Удалить deep-dive

**Файл**: `docs/brainstorms/worker-isolation-deep-dive.md`

Удалить — дублирует основной brainstorm, всё полезное (Sysbox, coordinator/agent split) либо не в scope, либо уже задокументировано.

### 4.3 Обновить ARCHITECTURE.md (если упоминает сетевую топологию)

Проверить и обновить описание сетей если есть.

### Критерии приёмки Iteration 4

- [ ] `worker-isolation-deep-dive.md` удалён
- [ ] `worker-db-isolation.md` обновлён (Phase 1 помечена как done)
- [ ] Документация не ссылается на `project-db` workaround как на текущий механизм
