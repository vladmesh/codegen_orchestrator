# Brainstorm: Изоляция БД оркестратора от воркеров

> **Дата**: 2026-03-03
> **Контекст**: E2E тест Level C показал, что агент внутри воркера может (и реально пытается) подключиться к postgres оркестратора вместо postgres проекта. Причина — DNS-коллизия: имя `db` резолвится в БД оркестратора, потому что воркер сидит на той же сети `codegen_internal`.

---

## Как сейчас

### Сетевая топология

```
codegen_internal (одна сеть на всё)
┌─────────────────────────────────────────┐
│  db (postgres оркестратора)              │
│  redis                                  │
│  api                                    │
│  langgraph, eng-worker, deploy-worker   │
│  worker-manager                         │
│  scheduler, infra-service               │
│  caddy, registry, telegram_bot          │
│                                         │
│  worker-dev-todo-api-xxx ───────────────┼──── dev_proj_xxx
│  worker-dev-echo-bot-yyy ───────────────┼──── dev_proj_yyy
└─────────────────────────────────────────┘
```

Воркер подключается к **двум** сетям:
1. `codegen_internal` — для доступа к redis, api, worker-manager
2. `dev_proj_{worker_id}` — для доступа к инфраструктуре проекта (postgres проекта, redis проекта)

### Проблема: DNS-коллизия `db`

Проект scaffolded с `POSTGRES_HOST=db` в `.env`. Сервис БД проекта тоже называется `db` в compose.

Из воркера `db` резолвится в **postgres оркестратора** (172.19.0.2 на `codegen_internal`), а не в postgres проекта (172.20.0.x на `dev_proj_*`).

### Текущий workaround: `project-db` alias

`compose_runner.py` добавляет alias `project-db` для сервиса `db` проекта на `dev_proj_*` сети. `orchestrator dev-env start-infra` патчит `.env`: `POSTGRES_HOST=db` → `POSTGRES_HOST=project-db`.

**Почему это плохо:**
- Хрупко: агент может вызвать `make migrate` до `start-infra` (`.env` ещё не пропатчен)
- Агент может "починить" `project-db` обратно на `db`, решив что это баг (E2E подтвердил)
- Молчаливая запись в чужую БД вместо громкой ошибки
- Лишний код: `_patch_db_hostname()`, alias-генерация, нестандартное имя хоста в `.env`

---

## Проблема 1: Сетевая изоляция

### Решение: отдельная сеть `codegen_worker`

```
codegen_internal              codegen_worker           dev_proj_{id}
┌──────────────────┐         ┌──────────────────┐     ┌──────────────┐
│  db (postgres)    │         │                  │     │ project-db   │
│  langgraph        │         │                  │     │ project-redis│
│  eng-worker       │         │                  │     └──────┬───────┘
│  deploy-worker    │         │                  │            │
│  scheduler        │         │                  │            │
│  infra-service    │         │                  │            │
│  caddy            │         │                  │            │
│  registry         │         │                  │            │
│  telegram_bot     │         │                  │            │
│                   │         │                  │            │
│  redis ───────────┼─────────┤ redis            │            │
│  api ─────────────┼─────────┤ api              │            │
│  worker-manager ──┼─────────┤ worker-manager   │            │
│                   │         │                  │            │
│                   │         │  worker ─────────┼────────────┘
└──────────────────┘         └──────────────────┘
```

Воркер подключается к `codegen_worker` (вместо `codegen_internal`) + `dev_proj_*`.

**Результат:**
- `db` из воркера → резолвится только на `dev_proj_*` → правильная БД проекта
- `redis`, `api`, `worker-manager` → доступны через `codegen_worker`
- Postgres оркестратора → невидим для воркера. Не DNS-хак, а физическая невозможность.

**Бонус:** `project-db` alias и `_patch_db_hostname()` больше не нужны. Убираем код, `.env` остаётся с нативным `POSTGRES_HOST=db`.

**Объём изменений:** ~6 строк в docker-compose.yml, ~2 строки в config.py, удаление ~30 строк мёртвого кода.

---

## Проблема 2: Ресурсы при параллельных воркерах

Каждый воркер поднимает свою инфраструктуру через `orchestrator dev-env start-infra`:
- Postgres контейнер (~100-200 MB RAM)
- Redis контейнер (~30 MB RAM)
- Возможно другие сервисы из compose проекта

5 параллельных воркеров = 5 postgres + 5 redis. На 16 GB машине терпимо, на 8 GB — предел.

### Подход A: Изолированные контейнеры (текущий)

Каждому проекту — свой postgres и redis в `dev_proj_*` сети.

| Плюсы | Минусы |
|-------|--------|
| Полная изоляция без усилий | ~250 MB RAM на воркер |
| Проект видит чистую БД | 5+ воркеров = 1.5+ GB только на инфру |
| Никаких credential-конфликтов | Долгий cold start (postgres init) |
| Уже работает | |

**Когда использовать:** до 3-5 параллельных воркеров. Текущая реальность.

### Подход B: Shared postgres, отдельные databases

Один postgres-контейнер для всех dev-проектов (не оркестраторский!). Каждому проекту создаётся отдельная database.

```
codegen_worker
┌──────────────────────────┐
│  dev-postgres (shared)    │   ← один контейнер
│    ├── db: todo_api_abc   │   ← database для воркера abc
│    ├── db: echo_bot_def   │   ← database для воркера def
│    └── db: weather_xyz    │
│  dev-redis (shared)       │
│  worker-abc               │
│  worker-def               │
│  worker-xyz               │
└──────────────────────────┘
```

Worker-manager при создании воркера:
1. `CREATE DATABASE project_{worker_id}`
2. `CREATE USER project_{worker_id} WITH PASSWORD '...'`
3. `GRANT ALL ON DATABASE ... TO ...`
4. Инжектит `POSTGRES_HOST=dev-postgres`, `POSTGRES_DB=project_{id}`, `POSTGRES_USER/PASSWORD` в `.env`

При удалении воркера:
1. `DROP DATABASE project_{worker_id}`
2. `DROP USER project_{worker_id}`

| Плюсы | Минусы |
|-------|--------|
| Один postgres на всех (~200 MB total) | Нужен "DB provisioner" в worker-manager |
| Быстрый старт (CREATE DATABASE vs container init) | Shared failure: postgres падает — все воркеры встают |
| Изоляция через pg credentials | Суперюзер всё равно видит все БД |
| Просто масштабируется | Нужно менять `.env` и compose проекта |

**Когда использовать:** 5+ параллельных воркеров, ограниченные ресурсы.

### Подход C: Shared postgres, разные schemas

Как B, но вместо отдельных databases — schemas в одной database.

| Плюсы | Минусы |
|-------|--------|
| Ещё меньше overhead | Слабая изоляция (один user = доступ ко всем schemas) |
| Одно подключение | Сложнее cleanup |
| | Ломает проекты которые рассчитывают на `public` schema |

**Вердикт:** Слишком хрупко. Если делать shared — лучше отдельные databases (подход B).

### Подход D: Гибрид — shared по умолчанию, изолированный по запросу

Worker-manager решает на основе конфига проекта:
- `backend` only → shared postgres (подход B)
- Сложный проект с кастомными extensions / несколькими БД → свой контейнер (подход A)

Нужен флаг в конфиге проекта или автодетект по `compose.base.yml`.

---

## Рекомендация

**Сейчас (Phase 1):** Сетевая изоляция (`codegen_worker`). Минимальные изменения, закрывает реальную проблему, убирает хрупкий workaround. Ресурсную оптимизацию откладываем.

**Потом (Phase 2, когда упрёмся в ресурсы):** Shared postgres для dev-окружений (подход B или D). Это отдельная задача с новыми компонентами (DB provisioner, credential management).

---

## Открытые вопросы

1. Нужен ли shared Redis для dev-проектов? Большинство backend-only проектов redis не используют. Те что используют — обычно для кеша, можно шарить с prefix isolation (`REDIS_KEY_PREFIX=project_{id}`).
2. Как обрабатывать кастомные compose-сервисы? Если проект определяет `elasticsearch` или `rabbitmq` в compose — они всегда поднимаются как изолированные контейнеры на `dev_proj_*`.
3. Health monitoring для shared postgres? Если он падает — все воркеры зависают. Нужен ли автоматический рестарт или алерт?
