# Brainstorm: User Dashboard (ЛК) — продуктовые метрики для фаундеров

> **Дата**: 2026-03-20
> **Контекст**: Фаундер (нетехнический) должен видеть продуктовые метрики своих проектов: юзеры, retention, p95, ошибки. Не Grafana, не админка — простой ЛК.
> **Связано с**: [product-analytics.md](product-analytics.md) — предыдущий brainstorm (prerequisites, structlog, middleware)
> **Status**: done

---

## Current State

### Что готово (prerequisites)

**service-template** (всё done + тесты):
- `shared/logging.py` — structlog, JSON stdout, service_name через contextvars
- `backend/src/app/middleware.py` — FastAPI request logging: method, path, status_code, duration_ms, user_id
- `tg_bot/src/middleware.py` — Telegram update logging: `tg:{id}`, update_type, command, duration_ms
- Error logging: exception_type, traceback, path, user_id

**Оркестратор**:
- Loki + Promtail в docker-compose (собирает логи оркестратора + worker-контейнеров)
- Grafana с дашбордом "Service Logs" (для админки)
- Ansible роль `monitoring/` — node-exporter + cadvisor на prod-серверах (метрики, НЕ логи)
- Loki retention: 7 дней, auth_enabled: false

**Модели**:
- `User` — telegram_id (unique), is_admin, owner проектов
- `Project` — owner_id FK → users.id, фильтрация по owner уже в API
- Auth: `X-Telegram-ID` header → lookup в БД
- Admin SPA: Basic Auth через nginx, отдельный от user auth

### Чего нет

1. Promtail на prod-серверах
2. Analytics агрегация
3. API для ЛК
4. ЛК фронтенд
5. Auth для ЛК (отличается от admin auth)

---

## Проблема

Фаундер сгенерировал проект, задеплоил, получил юзеров — и дальше слепой. Не знает:
- Пользуются ли вообще его продуктом
- Растёт ли аудитория или падает
- Тормозит ли сервис
- Сыпятся ли ошибки

Grafana не решает эту проблему — она для инженеров. Фаундеру нужно:
- Открыл → увидел 3 числа → понял ситуацию
- Без LogQL, без query builder, без промежуточных экранов

---

## Архитектура: pipeline данных

```
[prod-сервер]                    [оркестратор]
 container → stdout              Loki (уже есть)
           → Promtail (NEW) ──→  :3100
                                    │
                                 Aggregator (scheduler job, каждый час)
                                    │ LogQL queries → parse → aggregate
                                    ▼
                                 PostgreSQL (analytics_* таблицы)
                                    │
                                 API /api/analytics/...
                                    │
                                 ЛК фронтенд (отдельный SPA)
```

### Почему Loki → PostgreSQL, а не напрямую из Loki?

1. **Latency**: LogQL на 7 дней данных — секунды. ЛК должен открываться мгновенно.
2. **Retention**: Loki хранит 7 дней сырых логов. Агрегаты нужны за 90 дней.
3. **Кастомные метрики**: unique users, retention, p95 — это не нативные Loki операции, нужен постпроцессинг.
4. **Независимость**: если Loki упадёт или мы его заменим — ЛК продолжит работать на агрегатах.

---

## Модель данных

### Таблица: `analytics_hourly`

Один ряд = один час одного сервиса одного проекта.

| Поле | Тип | Описание |
|------|-----|----------|
| id | serial PK | |
| project_id | UUID FK | → projects.id |
| service_name | varchar | "backend", "tg_bot" |
| bucket | timestamptz | Начало часа (2026-03-20 14:00:00Z) |
| total_requests | int | Всего запросов |
| error_count | int | status_code >= 500 или level=error |
| unique_users | int | COUNT(DISTINCT user_id) |
| new_users | int | user_id, не встречавшийся ранее |
| p50_ms | float | Медиана duration_ms |
| p95_ms | float | 95-й перцентиль duration_ms |
| p99_ms | float | 99-й перцентиль duration_ms |
| top_endpoints | jsonb | [{path: "/start", count: 42}, ...] top-5 |
| created_at | timestamptz | |

**Индексы**: (project_id, bucket), (project_id, service_name, bucket)
**Retention**: 90 дней (scheduler job чистит старые)

### Таблица: `analytics_daily`

Свёртка из hourly. Один ряд = один день одного проекта (все сервисы суммированы).

| Поле | Тип | Описание |
|------|-----|----------|
| id | serial PK | |
| project_id | UUID FK | |
| date | date | 2026-03-20 |
| total_requests | int | SUM из hourly |
| error_count | int | SUM из hourly |
| unique_users | int | **Пересчёт**, не сумма (юзер мог быть в нескольких часах) |
| new_users | int | SUM из hourly (новый = новый, аддитивно) |
| dau | int | = unique_users (за этот день) |
| returning_users | int | unique_users, которые были хотя бы в одном предыдущем daily |
| p95_ms | float | Приближённый (worst-of-hourly-p95s) |
| error_rate | float | error_count / total_requests |
| created_at | timestamptz | |

**Retention**: 365 дней

### Таблица: `analytics_known_users`

Реестр "увиденных" user_id для расчёта new/returning.

| Поле | Тип | Описание |
|------|-----|----------|
| project_id | UUID FK | |
| user_id_hash | varchar | sha256(user_id) — не храним сырой tg:123 |
| first_seen | timestamptz | |
| last_seen | timestamptz | |

**PK**: (project_id, user_id_hash)

---

## Aggregator job

Scheduler job, запускается каждый час (в :05, чтобы Promtail дослал):

```python
async def aggregate_analytics():
    for project in await get_active_projects():
        for service in project.services:
            label = f'{service}' # определяется по Loki label

            # LogQL: все request/update логи за последний час
            logs = await loki_client.query_range(
                f'{{job="docker", compose_service=~"{label}"}} |= "event" | json',
                start=bucket_start,
                end=bucket_end,
            )

            # Агрегация в Python
            hourly = compute_hourly_metrics(logs)
            await upsert_analytics_hourly(project.id, service, bucket_start, hourly)

    # Daily свёртка (в полночь UTC)
    if is_midnight_run:
        await compute_daily_rollups()
        await cleanup_old_hourly(days=90)
        await cleanup_old_daily(days=365)
```

### Проблема: как Loki знает какой контейнер принадлежит какому проекту?

Сейчас Promtail на оркестраторе скрейпит по label `com.docker.compose.project=codegen_orchestrator`. На prod-серверах контейнеры сгенерированных проектов будут иметь свои compose project name.

**Нужно**: при деплое проставлять label `com.codegen.project_id=<uuid>` на контейнеры. Тогда Promtail на prod-сервере сможет скрейпить все контейнеры с этим label, а Loki будет иметь project_id как label для фильтрации.

---

## API для ЛК

### Аутентификация

ЛК доступен по ссылке из Telegram бота. Flow:
1. Фаундер нажимает "Мой дашборд" в боте
2. Бот генерирует одноразовый токен (UUID, TTL=5 мин), сохраняет в Redis: `lk_token:{token} → user_id`
3. Бот отправляет ссылку: `https://app.example.com/dashboard?token={token}`
4. ЛК фронтенд отправляет token на бэкенд, получает JWT (TTL=24h)
5. Дальше — стандартный JWT auth

Почему не Telegram Login Widget:
- Требует публичного домена с SSL для callback
- Сложнее интегрировать для пользователя (нужен Telegram на том же устройстве)
- Одноразовый токен через бота — проще и уже есть связка user↔bot

### Endpoints

```
POST   /api/lk/auth/token          — обмен одноразового токена на JWT
GET    /api/lk/projects             — список проектов фаундера
GET    /api/lk/projects/{id}/summary?period=24h|7d|30d
       → { total_users, new_users, dau, wau, returning_pct,
           total_requests, error_rate, p95_ms,
           top_endpoints: [...] }
GET    /api/lk/projects/{id}/chart?metric=users|requests|errors&period=7d
       → [{ date: "2026-03-20", value: 42 }, ...]
GET    /api/lk/projects/{id}/status
       → { services: [{ name: "backend", status: "up", last_seen: "..." }] }
```

---

## Фронтенд ЛК

### Отдельный SPA, НЕ раздел админки

Почему:
- Разная аудитория (фаундер vs мы)
- Разный auth (JWT через Telegram vs Basic Auth)
- Разный дизайн (минимальный vs информационно-плотный)
- Независимый деплой

### Stack
- React + Vite (как admin-frontend, унифицируем)
- Tailwind CSS
- Recharts (или Chart.js) для графиков
- Nginx в Docker (как admin-frontend)

### Экраны

**1. Список проектов**
```
┌─────────────────────────────────┐
│ Мои проекты                     │
├─────────────────────────────────┤
│ 🟢 Weather Bot     42 юзера    │
│    Requests: 1.2k/day  p95: 45ms│
│                                  │
│ 🟡 Todo App        3 юзера     │
│    Requests: 89/day    p95: 120ms│
└─────────────────────────────────┘
```

**2. Дашборд проекта**
```
┌──────────────────────────────────────────┐
│ Weather Bot          [24h] [7d] [30d]    │
├──────────────────────────────────────────┤
│                                          │
│  Юзеры     Запросы    Ошибки    p95      │
│   42        1.2k      0.3%      45ms     │
│  +5 new    ▲ 12%      ▼ ok      ▼ ok     │
│                                          │
│  ┌──── Юзеры за 7 дней ────┐             │
│  │    ╱╲    ╱╲              │             │
│  │   ╱  ╲  ╱  ╲   ╱╲       │             │
│  │  ╱    ╲╱    ╲  ╱  ╲      │             │
│  │ ╱           ╲╱    ╲     │             │
│  └──────────────────────┘             │
│                                          │
│  Популярные:                             │
│  /start  — 320 (26%)                     │
│  /weather — 280 (23%)                    │
│  /settings — 150 (12%)                   │
└──────────────────────────────────────────┘
```

---

## Зависимости и порядок

```
Phase 1: Инфра (можно начать сейчас)
├── 1a. Ansible: Promtail на prod-серверы → push в Loki оркестратора
├── 1b. Открыть Loki :3100 (Caddy + basic auth или firewall whitelist)
└── 1c. Docker label com.codegen.project_id при деплое

Phase 2: Backend (после Phase 1)
├── 2a. Модели: analytics_hourly, analytics_daily, analytics_known_users + миграции
├── 2b. Loki client (HTTP, LogQL queries)
├── 2c. Aggregator scheduler job (hourly)
└── 2d. Daily rollup job

Phase 3: API + Auth (после Phase 2)
├── 3a. One-time token auth flow (Redis + JWT)
├── 3b. /api/lk/ endpoints (summary, chart, status)
└── 3c. Telegram bot: кнопка "Мой дашборд"

Phase 4: Frontend (после Phase 3)
├── 4a. SPA: auth screen + project list + project dashboard
└── 4b. Docker image + Caddy route
```

---

## Decisions (2026-03-20)

- **Auth**: Basic Auth для MVP. Безопасность после бета-тестов.
- **Существующие проекты**: не мигрируем. Аналитика только для новых деплоев (с middleware + label).
- **Разбивка**: per-application (application = микросервис: backend, tg_bot). Проект = папка с applications.
- **unique_users**: точный COUNT DISTINCT, не HLL. Масштаб не тот.
- **p95 daily**: worst-of-hourly-p95. Грубо, для фаундера хватит.
- **WAU/MAU**: на лету из hourly (`SELECT COUNT(DISTINCT)` за 7/30 дней). Для MVP масштаб маленький.
- **Promtail на prod**: скрейпить только контейнеры с label `com.codegen.project_id`.
- **Loki auth**: Basic Auth через Caddy. Достаточно для MVP.

## Remaining Open Questions

1. **Название/домен**: `app.codegen.com/dashboard`? `lk.codegen.com`? Поддомен?
2. **Rate limiting**: отложено на post-MVP.

---

## Action Items

- → new task: "Ansible: deploy Promtail to prod servers, push to orchestrator Loki" — infra
- → new task: "Expose Loki :3100 with basic auth for prod-server Promtail" — infra
- → new task: "Add com.codegen.project_id label to deployed containers" — deploy node
- → new task: "Analytics models + migrations (hourly, daily, known_users)" — orchestrator
- → new task: "Loki HTTP client for LogQL range queries" — orchestrator
- → new task: "Aggregator scheduler job (hourly + daily rollup)" — orchestrator
- → new task: "ЛК auth: one-time token via bot → JWT exchange" — orchestrator
- → new task: "ЛК API endpoints (summary, chart, status)" — orchestrator
- → new task: "Telegram bot: 'Мой дашборд' button with one-time token link" — telegram
- → new task: "ЛК frontend SPA (project list + dashboard)" — new service
- → idea: "Grafana embedded panel in admin for the same analytics (reuse Loki data)" — admin
- → idea: "Push notifications: daily summary в Telegram ('у вас +5 юзеров вчера')" — future
