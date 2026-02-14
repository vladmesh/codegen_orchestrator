# Brainstorm: Service-template, Docker-in-Docker и Dev Environment для воркеров

> **Дата**: 2026-02-14
> **Контекст**: Обзор роли service-template в оркестраторе. Проблемы Docker-in-Docker при работе воркеров. Поиск архитектуры dev-окружения, которая сохраняет dev/prod parity без оверхеда DinD.

---

## Текущее состояние service-template

### Что делает

Copier-шаблон с встроенным фреймворком `.framework/`. Две основные функции:

1. **Spec-first кодогенерация**: YAML-спеки (models.yaml, events.yaml, domain specs) → 8 генераторов → Pydantic-схемы, FastAPI-роутеры, Protocol-контракты, контроллер-стабы, FastStream pub/sub, типизированные HTTP-клиенты.

2. **Docker-обвес**: tooling-контейнер (линтеры, тесты, генераторы), sync_services (генерация Dockerfile/compose из services.yml), CI workflow.

### Пайплайн генерации

```
shared/spec/models.yaml       → SchemasGenerator    → shared/shared/generated/schemas.py
shared/spec/events.yaml       → EventsGenerator     → shared/shared/generated/events.py
services/*/spec/*.yaml         → RoutersGenerator    → services/*/src/generated/routers/*.py
                               → ProtocolsGenerator  → services/*/src/generated/protocols.py
                               → ControllersGenerator→ services/*/src/controllers/*.py (стабы)
                               → EventAdapterGenerator→ services/*/src/generated/event_adapter.py
services/*/spec/manifest.yaml → ClientsGenerator     → services/*/src/generated/clients/
                               → RegistryGenerator   → shared/shared/generated/registry.py
```

### Как используется в оркестраторе

1. **Scaffolder**: `copier copy gh:vladmesh/service-template <repo> --data modules=... --data project_name=...`
2. **Copier update**: `copier update --defaults --trust` + `sync_services create`
3. **Developer-воркер**: клонирует сгенерированный проект, реализует бизнес-логику в контроллерах

---

## Проблемы spec-first подхода

### Двойная абстракция

LLM-агент умеет писать FastAPI-роутеры, Pydantic-модели, тесты напрямую. Заставлять его описывать всё в YAML, запускать генерацию, потом реализовать контроллеры — три шага вместо одного. Код, написанный агентом напрямую, будет лучше, потому что агент видит полный контекст, а не формат спеки.

### Хрупкость

Spec-формат — ещё один язык, который агент должен знать. Ошибка в YAML → непонятная ошибка генератора → агент тратит время на дебаг вместо написания кода.

### Ограниченность

Spec покрывает CRUD + pub/sub. Нестандартное (WebSocket, streaming, middleware, custom auth) — агент всё равно пишет руками, но внутри сгенерированного скелета, который может мешать.

### Maintenance burden

`.framework/` копируется внутрь каждого проекта. Обновление через `copier update` + `sync_services create`. Изменение в генераторах = миграция всех проектов.

### Толщина фреймворка

8 генераторов, spec loader с валидацией, Jinja2 templates, sync_services, compose blocks — свой мета-фреймворк поверх FastAPI. Каждый баг в генераторе = баг во всех проектах, но с лагом обнаружения.

### Вывод

Spec-first генерация в текущем виде приносит больше проблем, чем пользы. Claude Code и Factory.ai справляются с FastAPI без промежуточного YAML-слоя.

---

## Проблема Docker-in-Docker

### Текущая архитектура

```
Host Docker
  └── worker-manager (монтирует /var/run/docker.sock)
       └── worker-container (Claude Code)
            └── Внутри: make dev-start → docker compose up
                 └── Docker через socket passthrough
                      └── db, redis, service containers
                           └── Volumes: пути хоста ≠ пути контейнера
```

### Проблемы

1. **Volumes**: Worker делает `docker compose up` через хостовый socket. Docker daemon создаёт bind-mount'ы от путей хоста, но файлы лежат внутри worker-контейнера. Пути не совпадают.

2. **Networking**: Контейнеры, созданные worker'ом, живут в сети хоста, не worker'а. DNS не резолвится. В интеграционных тестах захардкожены IP-адреса (172.29.0.10, 172.29.0.20).

3. **CI**: GitHub Actions уже запускает workflow в контейнере. Docker внутри CI = Docker-in-Docker-in-Docker.

4. **Sysbox**: Решает проблему (свой dockerd на контейнер, volumes работают), но добавляет overhead (~100-200MB RAM на daemon) и операционную сложность.

---

## Предлагаемое решение: разделение задач

### Три потребности, три решения

| Задача | Docker сейчас | Реальная потребность | Решение |
|--------|--------------|---------------------|---------|
| Изоляция зависимостей между сервисами | Каждый сервис в своём контейнере | Отдельные Python-окружения | `uv sync` per-service |
| Инфраструктура (db, redis) | docker compose up | Работающий PostgreSQL и Redis | Оркестратор поднимает рядом |
| Запуск стека для тестирования | docker compose up | HTTP-эндпоинты | uvicorn нативно |

### uv для зависимостей

`uv` создаёт venv и ставит зависимости за секунды. Каждый сервис — свой venv:

```bash
cd services/backend && uv sync        # 2-3 секунды
cd services/tg_bot && uv sync         # 2-3 секунды
cd services/notifications && uv sync  # 2-3 секунды

# Тесты
cd services/backend && uv run pytest tests/unit/
```

`uv run` автоматически активирует правильный venv. Никаких конфликтов, никакого Docker.

### Инфраструктура: оркестратор поднимает рядом с воркером

Ключевой сдвиг: воркер не поднимает базу — worker-manager даёт ему готовую.

```
Worker-manager (имеет /var/run/docker.sock):
  1. Читает docker-compose.yml проекта
  2. docker compose up db redis    ← на ХОСТОВОМ Docker
  3. Создаёт worker-контейнер в ТОЙ ЖЕ docker-сети
  4. Передаёт DATABASE_URL, REDIS_URL через env vars

Worker (контейнер):
  1. uv sync
  2. pytest — работает с PostgreSQL из compose проекта
  3. uvicorn — поднимает сервис, дёргает ручки
  4. Никакого Docker внутри
```

Инфра-контейнеры запускаются из docker-compose.yml **самого проекта** — тот же образ, те же конфиги, те же настройки. Dev/prod parity сохраняется.

### Итоговая картина

```
Host Docker daemon
│
├── orchestrator network (api, redis, langgraph, ...)
│
├── dev-proj-abc123 network          ← per-project сеть
│   ├── db        (postgres:16)      ← из docker-compose.yml проекта
│   ├── redis     (redis:7-alpine)   ← из docker-compose.yml проекта
│   └── worker-abc123                ← Claude Code agent
│       ├── uv sync                  ← зависимости нативно
│       ├── pytest                   ← тесты с реальной базой
│       └── uvicorn                  ← может поднять сервис
│
├── dev-proj-def456 network          ← другой проект, полная изоляция
│   ├── db
│   ├── redis
│   └── worker-def456
```

---

## Dev/prod parity

### Что сохранено

- Инфра из того же compose — те же образы, те же конфиги, те же версии
- Изоляция между проектами — отдельные сети, отдельные базы
- Агент может поднять сервис и подёргать ручки

### Где gap

Сервисы приложения: при разработке запускаются нативно через `uv run uvicorn`, в проде — в Docker-контейнерах. Разница минимальна для Python web-сервисов (одна версия Python, одни зависимости из lockfile, одна база).

### Tiered testing

```
Уровень 1: Юнит-тесты (90% работы агента)
  → Моки/SQLite/fakeredis или реальная БД от оркестратора
  → 0 Docker overhead для агента

Уровень 2: Интеграционные тесты (9% работы)
  → Shared PostgreSQL + Redis (от оркестратора)
  → Сервисы запущены нативно через uvicorn
  → Достаточная parity для 99% кейсов

Уровень 3: Full-stack валидация (1%, перед "done")
  → CI pipeline (GitHub Actions service containers)
  → docker compose из проекта
  → Полная parity
```

---

## Масштабирование

### Per-project инфра-контейнеры

| Пользователей | Контейнеров | RAM |
|--------------|------------|-----|
| 10 | 10 workers + 10 PG + 10 Redis = 30 | ~4.3GB |
| 100 | 300 | ~43GB |

### Shared infra (оптимизация при масштабе)

Один PostgreSQL-инстанс, отдельная БД на каждый проект:

| Пользователей | Контейнеров | RAM |
|--------------|------------|-----|
| 10 | 10 workers + 2 shared infra = 12 | ~3.5GB |
| 100 | 102 | ~30GB |

100 баз на одном PostgreSQL — штатная нагрузка. Теряем часть parity (shared PG instance вместо per-project), но экономим ресурсы. Можно начать с per-project и переключить на shared при росте.

При 100+ одновременных воркерах ограничение — стоимость LLM-вызовов и CPU, не инфра.

---

## Docker в проде — зачем он остаётся

Docker не нужен для изоляции зависимостей (это делает uv). Docker нужен для **деплоя и оркестрации**:

| Задача | С Docker | Без Docker |
|--------|----------|------------|
| Запуск | `docker compose up -d` | N × systemd unit файлов |
| Рестарт | `restart: unless-stopped` | systemd restart policy |
| Откат | `docker compose up -d` с прошлым тегом | git revert + uv sync + restart × N |
| Изоляция | Из коробки | Отдельные юзеры, cgroups |
| Деплой через Ansible | compose pull + up | git pull + uv sync + restart × N |

Для автоматизированного деплоя оркестратором Docker Compose — один Ansible-таск вместо десяти.

---

## Что делать с service-template

### Убрать

- Всю spec-first кодогенерацию (8 генераторов, spec loader, models.yaml/events.yaml format)
- sync_services и динамическую генерацию Dockerfile/compose
- Tooling-контейнер для линтеров и тестов

### Оставить и усилить

1. **Copier-шаблон как скелет проекта:**
   - Структура директорий (services/, shared/, tests/, infra/)
   - pyproject.toml с ruff, mypy, pytest конфигурацией и uv-совместимый
   - Makefile с двумя режимами: нативный (для агента) и Docker (для людей/CI)
   - .github/workflows/ci.yml
   - .gitignore, pre-commit hooks
   - CLAUDE.md / AGENTS.md с инструкциями для LLM-агентов

2. **Docker только для runtime:**
   - Простые статические Dockerfile'ы (multi-stage)
   - docker-compose.yml для инфры + сервисов

3. **Код-анализ и качество:**
   - Ruff (lint + format)
   - mypy (typecheck)
   - pytest + coverage
   - xenon (complexity)

### Шаблон превращается из "фреймворка для кодогенерации" в "starter kit с правильными конвенциями". Основная ценность — единообразная структура проектов и настроенный тулинг.

---

## Изменения в worker-manager

Worker-manager получает новую ответственность: **provisioning dev-окружения**.

```python
async def create_worker_with_env(self, command: CreateWorkerCommand):
    project_dir = await self._clone_project(command.repo_url)

    # 1. Поднять инфру проекта на хостовом Docker
    infra_network = f"dev-{command.project_id[:8]}"
    compose_services = self._get_infra_services(project_dir)  # ["db", "redis"]

    await self.docker.compose_up(
        project_dir=project_dir,
        services=compose_services,
        project_name=f"dev-{command.project_id[:8]}",
        network=infra_network,
    )

    # 2. Дождаться healthcheck'ов
    await self._wait_for_healthy(infra_network, compose_services)

    # 3. Собрать connection strings
    env_vars = {
        "DATABASE_URL": f"postgresql://app:app@db:5432/app",
        "REDIS_URL": f"redis://redis:6379/0",
    }

    # 4. Создать воркер в той же сети
    worker = await self.create_worker(
        command, extra_env=env_vars, network=infra_network,
    )
    return worker

async def delete_worker(self, worker_id: str):
    await super().delete_worker(worker_id)
    await self.docker.compose_down(project_name=f"dev-{project_id[:8]}")
```

---

## Worker-контейнер становится тонким

```
Было:
  Python + Git + curl + jq + ruff + copier + worker-wrapper
  + Docker CLI (capability) + весь оверхед DinD
  ~800MB+

Станет:
  Python + Git + uv + ruff + worker-wrapper
  ~200MB
```

---

## Открытые вопросы

- **Системные зависимости**: Если проект требует `ffmpeg`, `libmagic` и т.п. — как это обрабатывать? Прописывать в шаблоне? Давать агенту sudo apt-get?
- **Запуск Dockerfile проекта**: Стоит ли дать агенту возможность попросить worker-manager собрать и запустить Docker-образ сервиса рядом? Как опциональную фичу для тех случаев, когда нативный запуск не катит.
- **Shared vs per-project infra**: Начать с per-project (проще, полная parity) или сразу с shared (экономнее)?
- **CI workflow в шаблоне**: GitHub Actions service containers (нативные) vs docker compose в CI?
