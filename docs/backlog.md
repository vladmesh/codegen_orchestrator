# Backlog

> **Актуально на**: 2026-02-25

Мы используем итеративный подход. Этот бэклог консолидирует задачи из предыдущих аудитов, брейнштормов и планов. Приоритет отдан архитектуре, стабильности процессов разработки и закрытию техдолга (DevEx). Продуктовые фичи вынесены в конец.

---

## 🔴 HIGH Priority (Architecture, DevEx, Refactoring)

Фундаментальные изменения, отладка конвейера и оптимизация цикла разработки.

### 1. Service Template Simplification & Refactoring
**Документы**: `docs/brainstorms/service-template-and-dev-environment.md`
**Проблема**: Фреймворк `service_template` сильно перегружен абстракциями. 8 кодогенераторов, обязательный PostgreSQL для любого проекта.
**Задачи**:
- Избавиться от избыточных кодогенераторов (`RoutersGenerator`, `ClientsGenerator`, `RegistryGenerator`, `sync_services`).
- Оставить только contract-first генерацию (общесетевых моделей, схем эвентов) и начального пула папок. Внутреннюю логику агент пишет сам.
- Упростить `service_template` до опционального Backend/Postgres (для простых Telegram-ботов). (Cross-project: orchestrator scaffolder + template).

### 2. Agent Hierarchy & Incident Response Pipeline
**Документы**: `docs/brainstorms/agent-hierarchy.md`
**Проблема**: `PO-воркер` сейчас берёт на себя слишком много. Ошибки в пайплайне не исправляются умно, а падают с исчерпанием ретраев.
**Задачи**:
- **TaskAssessor & Architect**: Внедрить ноду Архитектора для сложных задач и TaskAssessor для первоначального анализа.
- **Scaffolder node**: Перевести Scaffolder из механического background-сервиса в ноду Engineering subgraph.
- **Watchdog & Recovery**: Добавить DockerEventsListener и DLQ consumer в scheduler + простые рекавери-плейбуки. Добавить механизм `request_help` для агента.
- **Shared Session Memory**: Транслировать ошибку и `stderr` от упавшего агента к новому процессу (retry) в `TASK.md` (предсмертная записка).

### 3. ~~Redis Streams: PEL Recovery & Унификация Consumer'ов~~ → ✅ Done
> Объединено с #5. См. [redis-streams-unification.md](plans/redis-streams-unification.md).

### 4. CI Pipeline Redesign & Integration Test Speedup
**Документы**: `docs/brainstorms/ci-pipeline-redesign.md`, `docs/brainstorms/integration-test-speedup.md`
**Проблема**: CI собирает и пушит образы в GitHub Container Registry даже если тесты упали. Тесты идут 10+ минут последовательно.
**Задачи**:
- Включить Branch Protection.
- Разделить CI на PR (только выполнение тестов и билд для проверки, без пуша) и Publish (на `main`).
- Запускать интеграционные тесты параллельно (Github Actions matrix).

### 5. ~~Queue Contract Enforcement~~ → ✅ Done
> Объединено с #3. См. [redis-streams-unification.md](plans/redis-streams-unification.md).

### 6. Security Audit: Server Provisioning & Deploy
**Документы**: `docs/backlog.md`
**Проблема**: Деплой от рута, незакрытые порты, отсутствие удаляющего cleanup.
**Задачи**:
- Создать пользователя `deploy` без root прав. Ограничить SSH.
- Включить firewall, sshd hardening, fail2ban через ansible security роль.
- Очищать зависшие контейнеры / образы после окончания деплоев проекта (`docker image prune`).

---

## 🟡 MEDIUM Priority (Process Stability, Automation)

### 7. Workspace Failure Counter & Retry Limit (Persistence Phase 6)
**Документы**: `docs/plans/workspace-persistence.md`
Накопление числа падений воркера по `project_id`. Wipe workspace после 2 попыток (чтобы избежать застрявших merge conflicts / detached head). Отклонение после 3 попыток.

### 8. Worker Reuse for CI Fix Loop
**Документы**: `docs/backlog.md`
Не перезапускать новый контейнер, когда интеграционные тесты CI падают. Отправлять задачу на фикс в *тот же* инстанс воркера, чтобы сэкономить время стартапа.

### 9. Worker Lifecycle (Pause/Unpause, Limits)
**Документы**: `docs/tasks/worker-lifecycle.md`
Управление "простаивающими" воркерами: `docker pause` при бездействии. Также ввести CPU и RAM лимиты на контейнеры (запрет `MAX_CONCURRENT_WORKERS` монополизации).

### 10. E2E Тесты
Завершение покрытия системы E2E тестами (завершить неоконченные фазы 5-7).

### 11. Remove Obsolete Zavhoz
**Документы**: `docs/backlog.md`
Обновить документацию и конфигурацию. Полностью удалить `Zavhoz` — вместо него уже работает `ResourceAllocatorNode`.

### 12. Fix "Deploy-worker" Documentation
**Документы**: `docs/audit.md`
Отразить в документации, что `deploy-worker` и `engineering-worker` являются процессами LangGraph, а не скрытыми суб-сервисами.

---

## 🟢 LOW Priority (Product Features, Polish, Ad-Hocs)

Штуки, которые можно отложить до момента, когда разработка будет стабильной.

- **Admin UI**: Базовая админка (Projects, Workers, Logs) для дебага без CLI/Redis.
- **TesterNode (Ручное тестирование)**: Размещение тестер-агента после деплоя стейджинга или прода, чтобы он тыкал UI и API.
- **CI Monitor Node**: Вынесение мониторинга GitHub Actions failures (`_wait_for_ci_and_fix`) в прозрачную LangGraph-ноду.
- **API Authentication**: Замена `x-telegram-id` на вменяемый API Token или JWT.
- **Telegram Bot Pool**: Быстрая выдача пре-зарегистрированных Telegram ботов новым проектам (product).
- **Cost Tracking**: Мониторинг LLM-баланса, расчёты потраченных токенов на проект.
- **Deploy Rollback Capability**: Откат деплоя при failed health checks продакшена.
- **Docker Python SDK**: Миграция вызовов docker cli в subprocess`ах worker-manager'а на официальный Docker SDK.

---

### 13. Contract Consistency Improvements (Остаток #3+#5)
**Документы**: [redis-streams-unification.md](plans/redis-streams-unification.md) → «Остаточные замечания»
**Проблема**: После унификации consumer'ов остались мелкие несоответствия — часть publish-вызовов идёт через raw `redis.xadd`, а не через `client.publish_flat()`; несколько consumer'ов не валидируют входящие данные Pydantic-контрактом.
**Задачи**:
- ProactiveListener — добавить `POProactiveMessage` валидацию на consume
- Infra Service consumer — добавить `ProvisionerMessage.model_validate()` на consume
- Telegram bot PO publish — перевести с raw `redis.xadd` на `client.publish_flat()`
- Reminders publish — перевести с raw `redis.xadd` на `client.publish_flat()`
- PO tools `trigger_engineering` — перевести с raw `redis.xadd` на `client.publish_message()`
- Infra service result publish — перевести на `client.publish_message()`

---

## 🗑️ Completed / Superseded
*Архив или уже имплементированные решения*

- **Secrets Encryption**: Superseded by Fernet encryption in *Iter 1*.
- **Caddy Reverse Proxy**: Done in deploy-architecture *Iter 7*.
- **PO ReactAgent без контейнера**: Done. Переход на API-based ReactAgent завершен.
- **Dev Environment Docker-in-Docker Migration**: Фазы 1-4 завершены. (В планах осталось только E2E тестирование).
- **Redis Streams: PEL Recovery & Consumer Unification (#3+#5)**: Done. 9 consumer'ов переведены на `RedisStreamClient.consume()` с PEL recovery. Pydantic контракты на все очереди. См. [redis-streams-unification.md](plans/redis-streams-unification.md).
