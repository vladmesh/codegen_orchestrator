# Brainstorm: API Visibility & Scoping

> **Дата**: 2026-04-09
> **Контекст**: API полностью открыт внутри Docker network. 100+ эндпоинтов без auth. Цель — не безопасность (нет живых юзеров), а системность и снижение когнитивной нагрузки. Принцип: "если тебе это не нужно, ты это не видишь и не можешь увидеть."
> **Status**: done

---

## Current State

### Кто вызывает API

| Сервис | Роль | Что вызывает | Auth |
|--------|------|-------------|------|
| **langgraph** | Оркестрация агентов | stories, tasks, projects, servers, applications, deployments, rag, incidents, allocations | Нет |
| **scheduler** | Диспетчер/cron | stories, tasks, runs, projects, servers, applications, incidents, analytics, api-keys, users | Нет |
| **infra-service** | Provisioning серверов | servers (включая SSH ключи), applications | Нет |
| **telegram-bot** | User-facing бот | users, projects, rag | X-Telegram-ID (partial) |
| **worker containers** | Не вызывают API напрямую | — | — |
| **admin UI (lk)** | Дашборд юзера | lk/* endpoints | JWT Bearer |

### Ключевое наблюдение

Worker-контейнеры **не вызывают API**. Они общаются через:
- `localhost:9090/result` → worker-wrapper → Redis Stream
- `localhost:9090/infra/compose` → worker-manager proxy

Значит проблема scoping'а воркеров — **уже решена архитектурно**. Worker не может дёрнуть API, потому что у него нет к нему доступа (только localhost wrapper).

Остаются 4 внутренних сервиса + telegram-bot + admin UI.

---

## Problem / Opportunity

### Что реально болит

1. **Нет контракта "сервис X видит Y"**. Каждый сервис _фактически_ вызывает ограниченный набор эндпоинтов (через типизированный клиент), но это не enforced. Завтра scheduler может случайно вызвать `/api/servers/{handle}/ssh-key` — и ничто его не остановит.

2. **Нет audit trail**. Когда в БД оказывается мусор — непонятно кто вызвал какой эндпоинт. Нет способа ответить на вопрос "кто удалил этот проект?".

3. **Деструктивные эндпоинты без защиты**. 15+ DELETE эндпоинтов доступны любому контейнеру. SSH ключи (`GET /servers/{handle}/ssh-key`) отдаются без вопросов.

4. **Оператору приходится держать в голове** что какие сервисы вызывают. Нет единого места где описано "scheduler может X, langgraph может Y".

### Что НЕ болит (пока)

- Multi-tenancy — один оператор, один юзер.
- Атаки извне — API не экспонирован наружу.
- Worker scoping — воркеры не вызывают API.

---

## Options

### Option A: Service Tokens + Role-based endpoint whitelist

Каждый сервис получает статический токен (env var). API проверяет токен → определяет role → проверяет whitelist.

```python
# Роли
class ServiceRole(str, Enum):
    ORCHESTRATOR = "orchestrator"   # langgraph — полный доступ к pipeline
    SCHEDULER = "scheduler"         # scheduler — dispatch, monitoring, analytics
    INFRA = "infra"                 # infra-service — серверы, provisioning
    TELEGRAM = "telegram"           # telegram-bot — users, projects (scoped by user)
    ADMIN = "admin"                 # admin UI — всё (через JWT)

# Конфиг: role → разрешённые route prefixes
ROLE_PERMISSIONS = {
    ServiceRole.ORCHESTRATOR: [
        "/api/stories/*", "/api/tasks/*", "/api/projects/*",
        "/api/servers/*", "/api/applications/*", "/api/service-deployments/*",
        "/api/rag/*", "/api/incidents/*", "/api/allocations/*",
        "/api/repositories/*",
    ],
    ServiceRole.SCHEDULER: [
        "/api/stories/*", "/api/tasks/*", "/api/runs/*",
        "/api/projects/*", "/api/servers/*", "/api/applications/*",
        "/api/incidents/*", "/api/analytics/*", "/api/api-keys/*",
        "/api/users/*",
    ],
    ServiceRole.INFRA: [
        "/api/servers/*", "/api/applications/*",
    ],
    ServiceRole.TELEGRAM: [
        "/api/users/*", "/api/projects/*", "/api/rag/*",
    ],
}

# Middleware
async def verify_service_token(request: Request):
    token = request.headers.get("X-Service-Token")
    if not token:
        raise HTTPException(401)
    role = TOKEN_TO_ROLE[token]  # crash if unknown token
    if not matches_whitelist(request.url.path, ROLE_PERMISSIONS[role]):
        raise HTTPException(403, f"Role {role} cannot access {request.url.path}")
    request.state.service_role = role
```

- (+) Явный контракт: одна таблица "кто что может"
- (+) Audit trail: каждый запрос подписан сервисом
- (+) Простая реализация: middleware + dict
- (+) Токены генерятся при `make seed`, раздаются через env vars
- (-) Ещё один секрет на сервис (env var)
- (-) Route-level granularity — `GET /servers/{handle}` и `GET /servers/{handle}/ssh-key` оба под `"/api/servers/*"`. Для fine-grained нужен method+path whitelist
- (-) Whitelist нужно поддерживать при добавлении новых эндпоинтов

### Option B: Method+Path whitelist (fine-grained)

Как Option A, но whitelist включает HTTP метод.

```python
ROLE_PERMISSIONS = {
    ServiceRole.INFRA: [
        ("GET", "/api/servers/{handle}"),
        ("PATCH", "/api/servers/{handle}"),
        ("GET", "/api/servers/{handle}/ssh-key"),
        ("GET", "/api/servers/{handle}/applications"),
    ],
    # ...
}
```

- (+) Максимальная точность — infra видит ssh-key, scheduler нет
- (-) Огромный конфиг: 4 сервиса × ~25 endpoints каждый = ~100 строк
- (-) Каждый новый эндпоинт = обновить whitelist + все клиенты
- (-) Хрупкий: забыл добавить permission → 403 в проде → дебаг

### Option C: Network-level isolation (Docker networks per service)

Вместо API auth — разные Docker networks. Каждый сервис видит только нужные порты API.

```yaml
# docker-compose.yml
networks:
  orchestrator-api:     # langgraph + api
  scheduler-api:        # scheduler + api
  infra-api:            # infra-service + api
  telegram-api:         # telegram-bot + api
```

- (+) Zero code changes — чистая инфра
- (+) Нет overhead на каждый запрос
- (-) Не решает scoping внутри API — если scheduler видит api:8000, он видит ВСЕ эндпоинты
- (-) Не даёт audit trail
- (-) Docker network per service = комбинаторный взрыв сетей
- (-) Не решает проблему когнитивной нагрузки — контракты неявные

### Option D: Typed API clients as the only enforcement (status quo + audit)

Не добавлять auth. Вместо этого:
1. Добавить `X-Service-Name` header в каждый клиент (не для auth, для audit)
2. Логировать каждый запрос с service_name
3. Полагаться на типизированные клиенты как enforcement

- (+) Ноль изменений в API
- (+) Audit trail через логи
- (-) Не enforcement — любой может подставить любой header
- (-) Не решает "если тебе не нужно — ты не видишь"
- (-) Новый разработчик может добавить метод в wrong client

---

## Analysis

### Что принцип "не нужно → не видишь" реально значит для каждого актора

| Актор | Что НЕ нужно видеть | Почему |
|-------|---------------------|--------|
| **infra-service** | tasks, stories, runs, analytics, users, rag, brainstorms | Знает только про серверы и приложения |
| **scheduler** | ssh-key, debug, agent-configs, brainstorms | Dispatch + monitoring, не инфра-секреты |
| **telegram-bot** | servers, tasks, stories, runs, analytics, incidents | Знает только про юзеров и проекты юзера |
| **langgraph** | analytics, debug, users, api-keys, system-configs | Pipeline orchestration, не admin/analytics |

### Granularity trade-off

Option A (prefix whitelist) покрывает 90% пользы за 20% сложности. Главный gap: `GET /servers/{handle}` vs `GET /servers/{handle}/ssh-key` — оба под `/api/servers/*`.

Но реально `ssh-key` нужен только langgraph (для deploy) и infra-service (для provisioning). Scheduler'у не нужен. Это **один** sensitive endpoint, не систематическая проблема.

Решение: `/api/servers/{handle}/ssh-key` — отдельное правило в whitelist, остальное по prefix.

### Complexity budget

Option A требует:
1. Middleware (~30 lines)
2. Permissions dict (~40 lines)
3. Token generation в seed (~10 lines)
4. Env var в каждый сервис (4 сервиса × 1 var)
5. `X-Service-Token` header в каждый API client (4 клиента × 1 line)

Итого: ~80 строк кода + 4 env vars. Разумный бюджет.

Option B удваивает конфиг и добавляет хрупкость. Не стоит.

Option C не решает проблему. Option D не решает проблему.

---

## Recommendation

**Option A (prefix whitelist) + точечные fine-grained правила для sensitive endpoints.**

Конкретно:

1. **Service tokens**: статические, генерятся `make seed`, передаются через env vars
2. **Middleware**: проверяет `X-Service-Token`, определяет role, проверяет prefix whitelist
3. **Whitelist**: по route prefix (`/api/servers/*`) + отдельные правила для sensitive (`GET /api/servers/*/ssh-key` → только ORCHESTRATOR, INFRA)
4. **Audit**: логировать `service_role` в structlog на каждый запрос
5. **Dev escape hatch**: в dev mode (`ENVIRONMENT=development`) можно пропускать без токена — чтоб не ломать `curl` из терминала
6. **LK endpoints**: остаются на JWT, не меняются

### Чего НЕ делать

- Не делать per-endpoint method+path whitelist — слишком хрупко
- Не менять Docker networks — не решает проблему
- Не добавлять OAuth/JWT для service-to-service — overkill для internal API
- Не scope'ить по project_id/story_id — это multi-tenancy, отдельная задача (bs-8437c4b3)

---

## Open Questions

1. **Где хранить permissions?** Hardcode в коде (dict) или в DB (system_configs)?
   - Рекомендация: hardcode. Permissions — это контракт, не runtime config. Менять через PR, не через UI.

2. **Что делать с debug endpoints?** `/api/debug/*` — оставить открытым в dev, заблокировать в prod?
   - Рекомендация: отдельная role `DEBUG` или просто `ENVIRONMENT != production` check.

3. **Health endpoint** `/health` — без auth всегда (для Docker healthcheck).

4. **Нужен ли rate limiting?** Нет. Все клиенты внутренние, мы их контролируем.

---

## Action Items

- → new task: "API service tokens + role-based prefix whitelist middleware" — core implementation: middleware, permissions dict, token seed, env vars, client headers, structlog audit
- → backlog #1022 — это и есть задача, нужен /plan по рекомендации выше
