# Аудит архитектуры

> **Дата**: 2026-02-11
> **Контекст**: Результаты ревизии документации и кода

---

## 🟡 Средние проблемы

### 1. Deploy-worker и engineering-worker — неявные сервисы
**Где**: `docker-compose.yml`
**Проблема**: `deploy-worker` и `engineering-worker` фактически — отдельные инстансы `langgraph` с другим `command`. В документации они не всегда упоминаются отдельно.
**Рекомендация**: Явно документировать как worker-процессы сервиса langgraph.

---

## 🟢 Низкоприоритетные

### 2. Developer Queue Routing отличается от спецификации
**Где**: `services/worker-manager/`
**Проблема**: Спецификация описывала per-worker unique queues, реализация использует shared queue.
**Влияние**: Минимальное — текущая реализация работает корректно.

---

> **Примечание**: Остальные пункты из предыдущего аудита перенесены в [backlog.md](./backlog.md) как конкретные задачи:
> TesterNode, API Authentication, Resource Limits, Idle Pause/Wakeup, Creation Queue, E2E тесты, удаление мёртвого кода (analyst.py/zavhoz.py).
