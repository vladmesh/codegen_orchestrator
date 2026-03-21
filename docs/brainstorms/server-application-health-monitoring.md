---
id: bs-69482380
status: triaged
title: "Server & Application Health Monitoring"
created_at: 2026-03-16T22:48:02.743576Z
---

# Brainstorm: Server & Application Health Monitoring (Infra)

> **Дата**: 2026-03-17
> **Контекст**: health_checker — скелет, данные только из API провайдера, нет реального мониторинга серверов и приложений
> **Связано с**: [product-analytics.md](product-analytics.md) — пользовательские метрики и ЛК (выделено в отдельный brainstorm)
> **Status**: done

---

## Current State

Что есть сейчас:
- **server_sync** (scheduler) дёргает Time4VPS API каждые 60с (список) / 5мин (метрики)
- Метрики из провайдера: `capacity_cpu/ram/disk`, `used_ram_mb`, `used_disk_mb`
- Проблемы с данными провайдера:
  - RAM считается плохо (часто показывает ~92% на сервере с 4GB, хотя реально меньше)
  - Нет CPU usage (только количество ядер)
  - Нет сетевых метрик
  - Нет данных о процессах/контейнерах
- **health_checker.py** — пустой `while True: await asyncio.sleep(60)`, ничего не делает
- **Incident model** существует в БД, но инциденты никогда не создаются автоматически
- **Application model** — `last_health_check` никогда не обновляется
- **Админка** показывает RAM/disk bars + статус + applications в раскрывающихся рядах. CPU cores в БД, но не рендерится

## Problem / Opportunity

1. **Нет реальной видимости** — узнаём о проблемах когда пользователь жалуется или деплой падает
2. **Метрики провайдера неточные** — RAM usage врёт, CPU usage нет совсем
3. **Нет health checks приложений** — не знаем живы ли задеплоенные сервисы
4. **Нет алертинга** — инцидент-модель есть, но заполняется только вручную

## Архитектура

### Level 1: Server Health (node_exporter + cadvisor)
На каждом managed сервере: node_exporter (:9100) + cadvisor (:8080), UFW allow only orchestrator IP.
health_checker опрашивает по HTTP напрямую (без SSH).

**node_exporter** (системные метрики):
```
- CPU usage (%)       — per-core и total
- RAM used/total      — точные данные из /proc/meminfo
- Disk used/total     — per-mountpoint
- Load average        — 1m/5m/15m
- Uptime              — seconds
- Network errors      — rx/tx errors, drops, retransmits
- Disk I/O            — read/write latency, throughput
```

**cadvisor** (per-container метрики):
```
- Container CPU %     — по каждому контейнеру
- Container RAM       — usage, limit, cache
- Container network   — rx/tx bytes, errors per container
- Container status    — running, stopped, OOMKilled
```

### Level 2: Application Health (HTTP probes)
Для каждого задеплоенного сервиса:
```
- GET /health          → 200 = healthy, else = down
- Response time (ms)   — латентность
- SSL cert expiry      — через OpenSSL или requests
```

### Metrics History
- Таблица `server_metrics_history` — time series, retention 7 дней
- Автоматический cleanup: DELETE WHERE created_at < now() - interval '7 days'
- Позволяет графики "CPU за последний час/день" в админке

### Alerting
- Авто-создание инцидентов → уведомление админу (Telegram через PO agent)
- Типы: `SERVER_UNREACHABLE`, `RESOURCE_EXHAUSTED`, `SERVICE_DOWN`, `SSL_EXPIRING`

## Решение

**node_exporter + cadvisor на серверах, наш health_checker дёргает по HTTP.**

- Ставим node_exporter + cadvisor на каждый managed сервер (через provisioning)
- Порты 9100/8080 закрыты UFW, allow only orchestrator IP
- health_checker опрашивает по HTTP напрямую, без SSH, без Prometheus
- Парсим стандартный Prometheus text format
- Пишем в нашу БД (last value + history)
- SSH только для daily job (filesystem drift + docker prune)

**(+)** Точные метрики (node_exporter лучше чем парсить `free -m`)
**(+)** Per-container данные (cadvisor) — видим нагрузку каждого сервиса
**(+)** HTTP вместо SSH — быстрее, проще, меньше overhead
**(+)** Стандартный формат — если потом захотим Prometheus, exporters уже стоят
**(+)** ~70-100 MB overhead на prod-сервере — терпимо
**(-)** Нужно добавить в provisioning (установка + UFW rule)
**(-)** При смене IP оркестратора — обновить UFW на всех серверах

Prometheus можно добавить позже (exporters уже будут стоять), но пока обходимся своим polling + своей БД.

## План реализации

### Phase 1: Server Health via node_exporter + cadvisor

1. **Provisioning: установка node_exporter + cadvisor**
   - Добавить в Ansible provisioning playbook
   - node_exporter systemd service, bind 0.0.0.0:9100
   - cadvisor Docker container, bind 0.0.0.0:8080
   - UFW rules: allow orchestrator IP → 9100, 8080; deny rest
   - Проверка: curl из оркестратора → `/metrics` отвечает

2. **Prometheus text format parser**
   - Парсер стандартного `/metrics` формата (text/plain)
   - Извлечение нужных метрик из node_exporter (cpu, ram, disk, load, network, uptime)
   - Извлечение нужных метрик из cadvisor (per-container cpu, ram, network, status)
   - Результат: structured dict

3. **Расширение Server model + metrics history**
   - Новые поля на Server: `cpu_usage_pct`, `load_avg_1m/5m/15m`, `network_rx_errors`, `network_tx_errors`, `container_count_running`, `container_count_total`, `uptime_seconds`
   - `last_health_check` — уже есть, но не заполняется
   - Таблица `server_metrics_history` (server_handle, timestamp, metrics JSON) — retention 7 дней
   - Миграция

4. **Health checker worker**
   - Заполняем скелет в `health_checker.py`
   - Цикл: для каждого managed+active сервера → HTTP GET :9100/metrics + :8080/metrics → parse → update БД + append history
   - Авто-создание инцидентов: `SERVER_UNREACHABLE` (HTTP fail), `RESOURCE_EXHAUSTED` (RAM/disk > 90%)
   - Уведомление админу через Telegram при создании инцидента
   - Интервал: каждые 60с (уже есть env var)
   - Cleanup job: раз в сутки DELETE history старше 7 дней

5. **Админка: расширенная карточка сервера**
   - CPU usage bar (зелёный/жёлтый/красный)
   - Load average
   - Network errors counter
   - Per-container list (name, CPU%, RAM, status) — данные из cadvisor
   - Last health check timestamp с индикатором свежести
   - Incident history (уже есть endpoint, нужен UI)
   - Графики CPU/RAM/disk за последний час/день (из history таблицы)

### Phase 2: Application Health Probes

6. **HTTP health prober**
   - Для каждого Application с `status != NOT_DEPLOYED` → GET domain/health
   - Обновление `Application.status` и `last_health_check`
   - Инцидент `SERVICE_DOWN` при 3+ consecutive fails → уведомление админу
   - Response time tracking
   - SSL cert expiry check → инцидент `SSL_EXPIRING` за 7 дней до истечения

7. **Админка: application health**
   - Статус каждого сервиса (healthy/degraded/down)
   - Response time
   - SSL cert status
   - Uptime % за последние 24h (из history)

### Phase 3: Drift Detection & Garbage Collection

8. **Container drift detection (в health_checker, через cadvisor)**
   - cadvisor отдаёт список контейнеров → сравниваем с applications из API
   - Orphan (на сервере, нет в базе) → warning в админке
   - Ghost (в базе RUNNING, контейнера нет) → обновить статус → DOWN, warning в админке

9. **Daily SSH job: filesystem drift + docker prune**
   - Единственное SSH-подключение раз в сутки
   - `ls /opt/projects/` → сравнение с API → orphan папки → warning в логи (не в админку)
   - `docker system prune -af --filter "until=72h"` + `docker volume prune -f`

## Зависимости и риски

- **node_exporter + cadvisor** — нужно добавить в provisioning (Ansible)
- **UFW rules** — при смене IP оркестратора нужно обновить на всех серверах
- **prometheus_client parser** — или свой парсер, или библиотека `prometheus-client` для parse
- **asyncssh** — только для daily job (filesystem check + docker prune), не для метрик
- **Overhead на prod-серверах**: ~70-100 MB RAM, ~2% CPU (node_exporter + cadvisor)
- HTTP polling каждые 60с — минимальная нагрузка, без SSH overhead

## Action Items

- → new task: "Provisioning: install node_exporter + cadvisor + UFW rules" — Phase 1
- → new task: "Prometheus text format parser for node_exporter + cadvisor metrics" — Phase 1
- → new task: "Extend Server model with health metrics + metrics history table (7d retention)" — Phase 1
- → new task: "Implement health_checker worker (HTTP polling + auto-incidents + Telegram alerts)" — Phase 1
- → new task: "Admin UI: extended server health dashboard with per-container view + charts" — Phase 1
- → new task: "HTTP health prober for deployed applications + SSL expiry check" — Phase 2
- → new task: "Admin UI: application health status and response times" — Phase 2
- → new task: "Container drift detection via cadvisor (orphans/ghosts in health_checker)" — Phase 3
- → new task: "Daily SSH job: filesystem drift check + docker prune" — Phase 3
- → idea: "Prometheus migration path when server count > 10" — future
