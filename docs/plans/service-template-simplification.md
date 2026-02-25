# Service Template Simplification

> **Задача**: Backlog #1 — Service Template Simplification & Refactoring
> **Брейншторм**: `docs/brainstorms/service-template-and-dev-environment.md`
> **Cross-project**: основная работа в `service-template`, интеграция в оркестраторе (scaffolder)

---

## Проблема

Фреймворк `service-template` перегружен: 8 кодогенераторов, обязательный PostgreSQL,
тесная связка tg_bot → backend. Простой Telegram-бот невозможен без полного бэкенда,
а его разработка затягивается на десятки минут из-за лишней инфраструктуры.

---

## Текущее состояние связки backend ↔ tg_bot

`backend` захардкожен как обязательный модуль — в `copier.yml` валидация:
```
"{% if 'backend' not in modules %}backend module is required{% endif %}"
```

`tg_bot` зависит от backend тремя способами:

1. **Compose**: `depends_on: backend: service_started`
2. **manifest.yaml**: `consumes: backend/users` → генерируется `BackendClient`
3. **Shared schemas**: импортирует `UserCreate` и т.п. из `shared.generated.schemas`

Сейчас tg_bot — это фронтенд к бэкенду через Telegram. Standalone бот невозможен.

---

## Что убрать

### Генераторы (из 8 → 3-4)

| Генератор | Решение | Причина |
|-----------|---------|---------|
| **RoutersGenerator** | Убрать | Агент напишет роутер напрямую лучше и быстрее, видит контекст |
| **ClientsGenerator** | Убрать | При 2-3 сервисах клиент пишется за минуту. При standalone tg_bot не нужен |
| **RegistryGenerator** | Убрать | Механика на 10 строк, обёрнутая в генератор с Jinja-шаблоном и spec-парсингом |
| **sync_services** | Убрать | Перегенерирует все Dockerfiles/compose при каждом `copier update`. Достаточно одноразовой генерации |
| SchemasGenerator | Оставить | Контракт между сервисами (models.yaml → shared Pydantic-схемы) |
| EventsGenerator | Оставить | Pub/sub контракты между сервисами |
| EventAdapterGenerator | Оставить | Wiring подписок + handler stubs с типами |
| ControllersGenerator | Оставить | Стабы с типами — показывают агенту "вот что нужно реализовать" |

### Инфраструктурный оверхед

- **PostgreSQL + Alembic как обязательный** — для многих ботов хватит Redis или in-memory state
- **Tooling-контейнер** — отдельный Docker-образ для линтеров/тестов. Заменяется на `uv run pytest`
- **Spec-формат как обязательный слой** — для простого бота с 3 хендлерами описывать их в YAML → генератор → контроллер — три шага вместо одного

---

## Что оставить

- **SchemasGenerator** (models.yaml → shared Pydantic-схемы) — контракты между сервисами
- **EventsGenerator + EventAdapterGenerator** — pub/sub контракты
- **ControllersGenerator** — стабы с типами
- **Scaffold структуры** (папки, pyproject, Dockerfile, compose-блок) — одноразовая генерация

---

## Приоритеты реализации

| # | Что делать | Импакт | Сложность |
|---|------------|--------|-----------|
| 1 | Сделать backend опциональным в copier.yml (standalone tg_bot) | Высокий — разблокирует простых ботов | Средняя — пройтись по зависимостям |
| 2 | Убрать RoutersGenerator, ClientsGenerator, RegistryGenerator, sync_services | Высокий — упрощает шаблон вдвое | Низкая — удалить + обновить pipeline |
| 3 | Сделать specs опциональными (не требовать YAML для простых сервисов) | Средний — упрощает простые проекты | Средняя — ветка "с генерацией / без" |
| 4 | Убрать tooling-контейнер, перейти на `uv run` | Средний — ускоряет dev-цикл | Низкая |

---

## Связь с другими задачами бэклога

- **#2 (Agent Hierarchy)** — пересечение по pipeline (Architect node, Scaffolder-как-нода). Упрощение шаблона делается независимо; интеграция с новым pipeline — второй этап.
- **#4 (CI Pipeline Redesign)** — шаблон генерирует ci.yml для проектов. Слабое пересечение, не блокирует.
- Прямых блокеров нет, можно приступать.
