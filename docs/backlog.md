# Backlog

> **Актуально на**: 2026-03-01

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
- ~~**Scaffolder node**: Перевести Scaffolder из механического background-сервиса в ноду Engineering subgraph.~~ → ✅ Done (scaffolder удалён; copier выполняется в worker-manager scaffold phase, repo creation inline в engineering worker)
- **Watchdog & Recovery**: Добавить DockerEventsListener и DLQ consumer в scheduler + простые рекавери-плейбуки. Добавить механизм `request_help` для агента.
- **Shared Session Memory**: Транслировать ошибку и `stderr` от упавшего агента к новому процессу (retry) в `TASK.md` (предсмертная записка).

### 3. ~~Redis Streams: PEL Recovery & Унификация Consumer'ов~~ → ✅ Done
> Объединено с #5. См. [redis-streams-unification.md](plans/redis-streams-unification.md).

### 4. CI Pipeline Redesign & Integration Test Speedup
**Документы**: `docs/brainstorms/ci-pipeline-redesign.md`, `docs/brainstorms/integration-test-speedup.md`
**Проблема**: CI собирает и пушит образы в GitHub Container Registry даже если тесты упали. Тесты идут 10+ минут последовательно.
**Задачи**:
- Включить Branch Protection.
- ~~Разделить CI на PR (только выполнение тестов и билд для проверки, без пуша) и Publish (на `main`).~~ → ✅ Done
- Запускать интеграционные тесты параллельно (Github Actions matrix).

### 5. ~~Queue Contract Enforcement~~ → ✅ Done
> Объединено с #3. См. [redis-streams-unification.md](plans/redis-streams-unification.md).

### 6. ~~Migrate Pre-push Tests from Docker to Local venv~~ → ✅ Done
> Реализовано скриптом локального запуска в pre-push hook.

### 7. Security Audit: Project Deploy Cleanup
**Проблема**: Отсутствие удаляющей очистки после деплоев.
**Задачи**:
- Очищать зависшие контейнеры / образы после окончания деплоев проекта (`docker image prune`).
> *Часть с пользователем `deploy`, SSH hardening, fail2ban и UFW уже выполнена в ansible ролях.*

### 15. Resolve Enum Divergence between Models and DTOs
**Документы**: `docs/refactor-audit-v2.md` §3
**Проблема**: Enum-ы с одинаковыми именами, но **разными значениями** между `shared/models/` и `shared/contracts/dto/`. Это риск data integrity на границе API/DB.
- `ServerStatus`: модель — 11 значений (`ready`, `in_use`, `error`, `reserved`, `missing`, `decommissioned`…), DTO — 8 значений (`new`, `active`, `unreachable`…). Только 5 пересекаются.
- `IncidentStatus`: модель — `detected`/`recovering`/`resolved`/`failed`, DTO — `open`/`resolved`. Единственное пересечение — `resolved`.
- Модель имеет `IncidentType`, DTO имеет `IncidentSeverity` — разные концепции, не аналоги.
**Задачи**:
- Принять решение: DTO зеркалит модель (единый enum), или у них осознанно разные lifecycle (тогда нужен explicit маппинг).
- Привести в соответствие или задокументировать маппинг.

---

## 🟡 MEDIUM Priority (Process Stability, Automation)

### 8. Workspace Failure Counter & Retry Limit (Persistence Phase 6)
**Документы**: `docs/plans/workspace-persistence.md`
Накопление числа падений воркера по `project_id`. Wipe workspace после 2 попыток (чтобы избежать застрявших merge conflicts / detached head). Отклонение после 3 попыток.

### 9. ~~Worker Reuse for CI Fix Loop~~ → ✅ Done
> См. [worker-reuse-ci-fix.md](plans/worker-reuse-ci-fix.md). Wrapper multi-turn, spawner API (send_task/delete), engineering worker reuse с fallback, total gate timeout.

### 10. Worker Lifecycle (Pause/Unpause, Limits)
**Документы**: `docs/tasks/worker-lifecycle.md`
Управление "простаивающими" воркерами: `docker pause` при бездействии. Также ввести CPU и RAM лимиты на контейнеры (запрет `MAX_CONCURRENT_WORKERS` монополизации).

### 11. E2E Тесты
Завершение покрытия системы E2E тестами (завершить неоконченные фазы 5-7).

### 12. Remove Obsolete Zavhoz
**Документы**: `docs/backlog.md`
Обновить документацию и конфигурацию. Полностью удалить `Zavhoz` — вместо него уже работает `ResourceAllocatorNode`.

### 13. Fix "Deploy-worker" Documentation
**Документы**: `docs/audit.md`
Отразить в документации, что `deploy-worker` и `engineering-worker` являются процессами LangGraph, а не скрытыми суб-сервисами.

### 16. Consolidate `ServiceModule` (3 Sources of Truth)
**Документы**: `docs/refactor-audit-v2.md` §2.1
**Проблема**: Модули проекта (`backend`, `tg_bot`, `notifications`, `frontend`) определены в трёх местах:
1. `shared/contracts/dto/project.py` — `ServiceModule(StrEnum)`
2. `shared/schemas/modules.py` — `ServiceModule(StrEnum)` (идентичный дубль)
3. `packages/orchestrator-cli/src/orchestrator_cli/commands/project.py:22` — hardcoded `AVAILABLE_MODULES` список строк
**Задачи**:
- Оставить единый источник в `shared/contracts/dto/project.py`.
- Удалить `shared/schemas/modules.py`, заменить импорты.
- В CLI заменить hardcoded список на импорт enum.

### 17. Dead Code & Legacy Cleanup
**Документы**: `docs/refactor-audit-v2.md` §1
**Проблема**: Остатки прошлых рефакторингов, которые можно безопасно вычистить.
**Задачи**:
- ~~Удалить deprecated команду `update_framework` из `orchestrator-cli/commands/engineering.py`.~~ → ✅ Done (удалена; copier update признан ненужным, service-template — one-shot template).
- Удалить deprecated аргумент `--api-url` из `scripts/seed_agent_configs.py`.
- Убрать 4 redundant `import base64` в `services/worker-manager/src/manager.py` (строки 586, 608, 629, 677 — top-level import на строке 1 остаётся).
- Убрать legacy networking fallback в `manager.py:525-530` (если host networking больше не используется).
- Убрать legacy project lookup по имени в `scheduler/src/tasks/github_sync.py:213-226` (если все проекты уже слинкованы).

### 18. Split `engineering_worker.py` (947 LOC)
**Документы**: `docs/refactor-audit-v2.md` §4
Самый большой файл в кодовой базе. Вынести фазы (scaffold, CI fix loop, deploy trigger) в отдельные модули или helper-классы.

### 19. Split `github.py` Client (863 LOC)
**Документы**: `docs/refactor-audit-v2.md` §4
Разбить `shared/clients/github.py` на submodules по domain: repos, actions, secrets, workflows. Фасад `GitHubAppClient` делегирует в sub-clients.

### 20. API Key & SSH Key Encryption
**Документы**: `docs/refactor-audit-v2.md` §6.1
**Проблема**: API keys и SSH keys хранятся plain text несмотря на наличие Fernet-шифрования для project secrets.
- `services/api/src/routers/api_keys.py:36` — `TODO: Add real encryption here`
- `services/api/src/routers/api_keys.py:72` — `TODO: Add real decryption here`
- `services/api/src/routers/servers.py:66` — `TODO: Encrypt ssh_key`
**Задачи**:
- Применить существующий `SecretsCipher` (Fernet) к API key values и SSH keys.

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
- **Fix `sys.path` hack в telegram_bot**: `main.py:37` делает `sys.path.insert(0, "/app")` + 6 строк `noqa: E402`. Решить через PYTHONPATH в Docker или proper packaging.
- **Split Tier 2 large files**: `devops/nodes.py` (516), `telegram_bot/main.py` (473), `env_analyzer.py` (462), `server_sync.py` (411), `developer.py` (405) — разбивать по мере касания.
- **"Добавить батарейку" к существующему проекту**: Механизм добавления модулей (backend, notifications и т.д.) в уже развёрнутый проект. Подход: агент получает инструкцию сходить в service-template, посмотреть структуру нужного модуля и переиспользовать код/паттерны самостоятельно. `copier update` для этого не годится — шаблон не поддерживает инкрементальное добавление модулей.

---

### 14. ~~Contract Consistency Improvements (Остаток #3+#5)~~ → ✅ Done
> Вызовы `redis.xadd` заменены на `client.publish_message()`, pydantic контракты внедрены.

---

## 🗑️ Completed / Superseded
*Архив или уже имплементированные решения*

- **Secrets Encryption**: Superseded by Fernet encryption in *Iter 1*.
- **Caddy Reverse Proxy**: Done in deploy-architecture *Iter 7*.
- **PO ReactAgent без контейнера**: Done. Переход на API-based ReactAgent завершен.
- **Dev Environment Docker-in-Docker Migration**: Фазы 1-4 завершены. (В планах осталось только E2E тестирование).
- **Redis Streams: PEL Recovery & Consumer Unification (#3+#5)**: Done. 9 consumer'ов переведены на `RedisStreamClient.consume()` с PEL recovery. Pydantic контракты на все очереди. См. [redis-streams-unification.md](plans/redis-streams-unification.md).
- **Pre-push Tests to Local venv (#6)**: Done. Интегрирован быстрый локальный pytest скрипт без Docker overhead.
- **Security Audit Base (#7)**: Done. Пароли отключены, deploy юзер создан, fail2ban/UFW настроены.
- **Contract Consistency (#14)**: Done. Избавились от сырых вызовов `xadd` в пользу методов клиента.
- **StrEnum Migration**: Done. 21 instance `(str, Enum)` → `StrEnum` в 14 файлах (shared/contracts, shared/models, shared/schemas, services/langgraph).
- **Stale ruff.toml Cleanup**: Done. Удалены 3 per-file-ignores для несуществующих файлов (`product_owner.py`, `capabilities/base.py`, `worker.py`).
- **MockProcess Test Dedup**: Done. Вынесен в `packages/worker-wrapper/tests/conftest.py`.
