---
id: bs-f453edfb
status: done
title: "Product Analytics & User Dashboard"
created_at: 2026-03-16T23:24:03.193461Z
---

# Brainstorm: Product Analytics & User Dashboard

> **Дата**: 2026-03-17
> **Контекст**: Пользователи генерят проекты, но не видят аналитику (юзеры, ошибки, нагрузка). Нужен ЛК + стандартизация логов в service-template.
> **Связано с**: [server-health-monitoring.md](server-health-monitoring.md) — инфра-мониторинг (отдельный brainstorm)
> **Status**: draft

---

## Current State

- Сгенерированные проекты: бэкенд (FastAPI) + опционально бот (Telegram) + опционально фронтенд
- Бэкенд не обязательный — бот может работать без него
- structlog есть в оркестраторе, но НЕ в service-template
- Нет стандартного request middleware (каждый проект сам по себе)
- Нет аналитики: ни для нас (админка), ни для пользователя (ЛК)
- Grafana уже есть как часть админки

## Problem / Opportunity

Если мы хотим это как продукт — пользователь должен видеть:
- Сколько юзеров за последние сутки
- Что они делали (сложнее стандартизировать, зависит от домена)
- Среднее время ответа
- Количество ошибок (5xx)
- Warning'и

Для этого нужно:
1. Стандартизировать логирование на стороне service-template
2. Собирать эти логи централизованно
3. Показывать пользователю в ЛК

## Ключевое решение: где живёт ЛК

**ЛК — часть оркестратора (или отдельный сервис оркестратора), НЕ часть сгенерированного проекта.**

Почему:
- Держать отдельную версию ЛК для каждого пользовательского проекта — неподдерживаемо
- Обновления ЛК пришлось бы раскатывать на каждый сервер
- Данные всё равно собираются в оркестратор

Архитектура:
```
Сгенерированный сервис (на prod-сервере)
  ├── Request middleware → структурированный JSON stdout
  ├── GET /health → стандартный healthcheck
  └── (опционально) GET /analytics/summary → агрегат из своей БД

Оркестратор
  ├── Log collector → docker logs (через cadvisor API или Docker API) → парсинг JSON → наша БД
  └── ЛК (отдельный фронтенд или раздел в админке)
      → показывает данные собранные из логов сервисов пользователя

Пользователь
  └── Заходит в ЛК оркестратора → видит аналитику своего проекта
```

## Что нужно в service-template

### 1. Backend обязательный
Сейчас бот может быть без бэкенда. Для аналитики нужен бэкенд всегда — хотя бы как прокси для request logging. Или: middleware ставится и на бота тоже (логирует входящие update'ы).

**Решение**: не делать backend обязательным. Вместо этого — request middleware ставится на любой сервис (FastAPI-бэкенд или бот). Стандартизируем формат логов, а не архитектуру.

### 2. Structured log format
Стандартный JSON на stdout для каждого запроса:
```json
{
  "timestamp": "2026-03-17T12:00:00Z",
  "level": "info",
  "event": "request",
  "service": "weather-bot",
  "method": "POST",
  "path": "/webhook",
  "status_code": 200,
  "duration_ms": 45.2,
  "user_id": "tg:123456",
  "error": null
}
```

Что нужно в framework:
- structlog как зависимость (уже есть в оркестраторе, нужно в template)
- Request middleware для FastAPI — автоматический лог каждого запроса
- Update middleware для Telegram бота — лог каждого входящего update
- Стандартные поля: timestamp, level, event, service, method, path/command, status, duration, user_id

### 3. Error tracking
Стандартный формат для ошибок:
```json
{
  "timestamp": "2026-03-17T12:00:01Z",
  "level": "error",
  "event": "unhandled_exception",
  "service": "weather-bot",
  "exception_type": "ConnectionError",
  "exception_message": "Redis connection refused",
  "traceback": "...",
  "path": "/webhook",
  "user_id": "tg:123456"
}
```

## Что нужно в оркестраторе

### 1. Log collector
Варианты сбора логов с prod-серверов:

**Option A: docker logs через SSH/API**
- Периодически: `docker logs --since=1m <container>` → парсим JSON → пишем в нашу БД
- Просто, но polling-based, можно пропустить при задержке

**Option B: Loki + Grafana**
- Promtail на prod-сервере → Loki на оркестраторе → Grafana
- Индустриальный стандарт для логов, как Prometheus для метрик
- Grafana уже есть в админке
- Promtail ~30-50 MB RAM на prod-сервере

**Option C: stdout → Docker logging driver → remote**
- Docker logging driver (fluentd/gelf) → централизованный сбор
- Нужен дополнительный сервис (fluentd/logstash)

**Предварительно**: Option B (Loki) выглядит лучше — интегрируется с уже существующей Grafana, стандартный подход. Но это ещё один сервис на prod-серверах (Promtail).

### 2. Analytics aggregation
- Из собранных логов считаем: requests/day, unique users, error rate, avg response time
- Хранение: агрегаты в нашей БД (hourly/daily buckets)
- API endpoint для ЛК: GET /api/analytics/{project_id}/summary?period=24h

### 3. ЛК пользователя
- Минимальный фронтенд (или раздел в существующей админке с фильтром по user)
- Дашборд: requests, users, errors, response time
- Период: last 24h, 7d, 30d
- Grafana embedded dashboards (уже умеем, используем для логов в админке)

## Зависимости

- **service-template**: structlog + middleware — нужно сделать сначала
- **Инфра-мониторинг**: [server-health-monitoring.md](server-health-monitoring.md) — независимый, можно параллельно
- **Grafana/Loki**: дополнительная инфра, но Grafana уже есть
- Новые проекты получат middleware автоматически, существующие — нет (нужна миграция или ручное обновление)

## Open Questions

1. **Формат user_id**: для Telegram — `tg:{id}`, для API — что? JWT sub? API key?
2. **Retention**: сырые логи — 7 дней (как метрики)? Агрегаты — 90 дней?
3. **Privacy**: user_id в логах — ок для нашего use case, но нужно ли хэшировать?
4. **Существующие проекты**: как обновить уже задеплоенные проекты? copier update?
5. **Loki vs свой сбор**: Loki добавляет Promtail на каждый сервер (+30-50 MB). Стоит ли, или docker logs через SSH достаточно?

## Action Items

- → new task: "Add structlog + request middleware to service-template framework" — service-template
- → new task: "Add Telegram update logging middleware to service-template" — service-template
- → new task: "Standardize error log format in service-template" — service-template
- → new task: "Log collector: gather structured logs from prod servers" — orchestrator
- → new task: "Analytics aggregation service (hourly/daily buckets)" — orchestrator
- → new task: "User dashboard (ЛК) — basic analytics view per project" — orchestrator
- → idea: "Loki + Promtail for centralized log collection" — infra decision
- → idea: "copier update strategy for existing deployed projects" — migration
