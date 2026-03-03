# Backlog

> **Актуально на**: 2026-03-02

Мы используем итеративный подход. Этот бэклог консолидирует задачи из предыдущих аудитов, брейнштормов и планов. Приоритет отдан архитектуре, стабильности процессов разработки и закрытию техдолга (DevEx). Продуктовые фичи вынесены в конец.

---

## 🔴 HIGH Priority (Architecture, DevEx, Refactoring)

Фундаментальные изменения, отладка конвейера и оптимизация цикла разработки.

### 22. Worker Network Isolation (DNS Collision Fix)
**Документы**: `docs/plans/worker-network-isolation.md`, `docs/brainstorms/worker-db-isolation.md`
**Проблема**: Воркер сидит на `codegen_internal` вместе с postgres оркестратора. Имя `db` резолвится в БД оркестратора, а не проекта. Текущий workaround (`project-db` alias + `_patch_db_hostname()`) хрупок — агент может вызвать `make migrate` до патча или "починить" hostname обратно.
**Решение**: Новая сеть `codegen_worker` — воркеры физически не видят инфру оркестратора. Удаление workaround. ~40 строк изменений.
**Задачи**:
1. Создать сеть `codegen_worker`, подключить redis/api/worker-manager к обеим сетям
2. Переключить воркеров с `codegen_internal` на `codegen_worker`
3. Удалить `project-db` alias и `_patch_db_hostname()`
4. Тесты и валидация

### 1. Service Template Simplification & Refactoring
**Документы**: `docs/brainstorms/service-template-and-dev-environment.md`
**Проблема**: Фреймворк `service_template` сильно перегружен абстракциями. 8 кодогенераторов, обязательный PostgreSQL для любого проекта.

**Template-side (service-template repo):** ✅ **Всё выполнено**
- ~~Избавиться от избыточных кодогенераторов (`RoutersGenerator`, `ClientsGenerator`, `RegistryGenerator`, `sync_services`).~~ → ✅ Done (коммит `e924857`, 9→5 генераторов)
- ~~Оставить только contract-first генерацию (общесетевых моделей, схем эвентов) и начального пула папок.~~ → ✅ Done
- ~~Упростить `service_template` до опционального Backend/Postgres.~~ → ✅ Done (`modules=tg_bot` работает без DB)
- ~~Убрать tooling-контейнер.~~ → ✅ Done (per-service `.venv` + `uv`)

**Orchestrator-side (наша сторона):** ✅ **Всё выполнено**
- ~~`services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md:98` — ссылается на `make sync-services check`, которой больше нет в template. Убрать.~~ → ✅ Done
- ~~`services/langgraph/src/nodes/developer.py:355` — `make generate` → должно быть `make generate-from-spec`.~~ → ✅ Done
- ~~`services/langgraph/src/nodes/developer.py:349-350` — промпт всегда указывает на `shared/spec/models.yaml` и `events.yaml`, но при `modules=tg_bot` этих файлов нет. Сделать условным.~~ → ✅ Done
- ~~Облегчить worker-base image: убрать предустановленные ruff, xenon, pytest, mypy, copier и др. Перейти на `uv tool install` on-demand. Добавить uv-cache volume.~~ → ✅ Done (коммит `71eb9d1`, Iteration 4 из `plan-tooling-removal`)

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

### 6. Fix & Consolidate Test Suites
**Источник**: E2E Level C run 3 (worker-manager compose 500), аудит service/e2e тестов
**Проблема**: Service-тесты не в CI, не в хуках, часть сломана. E2E тесты деградировали.

**Makefile cleanup:** ✅ **Done** (коммит `2621eb4`)
- ~~Убрать Docker-based unit targets (`test-api-unit`, `test-langgraph-unit`, etc.).~~ → ✅ Done
- ~~Переименовать `test-unit-local` → `test-unit` (локально, без Docker).~~ → ✅ Done
- ~~Убрать `test-smoke`, `test-e2e`, `test-e2e-infra`, `test-e2e-worker-mock`, `test-service`, `test-all`.~~ → ✅ Done
- ~~Оставить `test-unit`, `test-integration`, `test-e2e-scaffold`, `test-clean`.~~ → ✅ Done
- ~~Починить `worker-manager/test_compose_api.py` (lifespan → isolated FastAPI), перенести в `tests/unit/`.~~ → ✅ Done
- ~~Удалить RED phase стабы: `langgraph/test_engineering_flow.py` (unit), `scheduler/test_provisioner_result_listener.py` (unit).~~ → ✅ Done

**Service-тесты (аудит 2026-03-02):**

*api* — ✅ OK, оставляем:
- `test_smoke.py` — DB + Redis connectivity smoke. Полезно.
- `test_pure_crud.py` — POST `/api/projects/` через ASGI client + DB. Реальный CRUD без side-effects.

*langgraph:*
- [ ] **`test_engineering_flow.py`** (service): RED phase — harness не реализован. **Удалить.**
- [ ] **`test_reminder_flow.py`**: тестирует `_poll_once()` с real Redis. Можно заменить Redis на FakeRedis и перенести в `tests/unit/`.

*scheduler* — ✅ OK (кроме RED phase):
- `test_github_sync_integration.py` — mock GitHub → `_sync_single_repo()` → real API. Настоящий integration.
- `test_server_sync_integration.py` — mock Time4VPS (respx) → `_sync_server_list()` → real API. Настоящий integration.
- [ ] **`test_provisioner_result_listener.py`**: RED phase — модуль `src.tasks.provisioner_result_listener` не реализован, 181 строка мёртвого кода. **Удалить.**

*telegram_bot:*
- [ ] **`test_notifications.py`**: `ProvisionerNotifier` с real Redis + mock bot. Можно заменить Redis на FakeRedis и перенести в `tests/unit/`. 3 теста с `asyncio.sleep(0.5)` — overhead.

*worker-manager:*
- [ ] **`test_flow.py`**: lifecycle/GC/auto-pause с FakeRedis + mock Docker. Чистый unit, перенести в `tests/unit/` (нужно скопировать фикстуры из conftest). 4 теста ERROR — API изменилось, нужно поправить.
- [ ] **`test_consumer.py`**: `WorkerCommandConsumer.process_message()` с FakeRedis + mock manager. Чистый unit. 1 failing test (`create_worker` не вызывается). Перенести + починить.

**E2E-тесты:**
- [ ] **`test_engineering_flow.py`**: `@pytest.mark.skip` навечно. Разблокировать или удалить.
- [ ] **Hardcoded IPs**: mock-anthropic `172.30.0.40:8000` в worker mock тестах. Заменить на DNS.
- [ ] **`test_real_llm.py`**: hardcoded `/host-claude` path (line 82), не использует `CLAUDE_SESSION_DIR`.
- [ ] **`test_dev_env_smoke.py`**: hardcoded `/tmp/codegen/workspaces` fallback.
- [ ] **CI**: E2E mock-тесты (Level A + B) не требуют credentials — можно добавить в CI.

### 7. Security Audit: Project Deploy Cleanup
**Проблема**: Отсутствие удаляющей очистки после деплоев.
**Задачи**:
- Очищать зависшие контейнеры / образы после окончания деплоев проекта (`docker image prune`).
> *Часть с пользователем `deploy`, SSH hardening, fail2ban и UFW уже выполнена в ansible ролях.*

### ~~15. Resolve Enum Divergence between Models and DTOs~~ → ✅ Done
> `ServerStatus` — единый enum в `shared/models/server.py` (superset: добавлены `NEW`, `ACTIVE`, `UNREACHABLE`). DTO реэкспортирует из модели. `shared/contracts/dto/incident.py` удалён как dead code (zero imports).

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
Текущий статус E2E описан в #6 выше. Ни один E2E тест не в CI.

### 12. Remove Obsolete Zavhoz
**Документы**: `docs/backlog.md`
Обновить документацию и конфигурацию. Полностью удалить `Zavhoz` — вместо него уже работает `ResourceAllocatorNode`.

### 13. Fix "Deploy-worker" Documentation
**Документы**: `docs/audit.md`
Отразить в документации, что `deploy-worker` и `engineering-worker` являются процессами LangGraph, а не скрытыми суб-сервисами.

### ~~16. Consolidate `ServiceModule` (3 Sources of Truth)~~ → ✅ Done
> Единый источник — `shared/contracts/dto/project.py`. Дубль `shared/schemas/modules.py` удалён. CLI и PO tools используют `ServiceModule` enum.

### 17. Dead Code & Legacy Cleanup
**Документы**: `docs/refactor-audit-v2.md` §1
**Проблема**: Остатки прошлых рефакторингов, которые можно безопасно вычистить.
**Задачи**:
- ~~Удалить deprecated команду `update_framework` из `orchestrator-cli/commands/engineering.py`.~~ → ✅ Done
- ~~Удалить deprecated аргумент `--api-url` из `scripts/seed_agent_configs.py`.~~ → ✅ Done
- ~~Убрать 4 redundant `import base64` в `services/worker-manager/src/manager.py`.~~ → ✅ Done
- Убрать legacy networking fallback в `manager.py:525-530` (активно используется в CI/e2e — **оставлено**, не мёртвый код).
- Убрать legacy project lookup по имени в `scheduler/src/tasks/github_sync.py:213-226` (защитный fallback — **оставлено**, предотвращает дубликаты проектов).

### 21. Deploy Pre-Check: Validate Server State Before Deploy
**Источник**: E2E Level C тесты (todo_api, 2026-03-02) — leftover deployment на сервере
**Проблема**: Deploy-воркер не проверяет состояние целевого сервера перед деплоем. Если на сервере остался `/opt/services/<PROJECT_NAME>/` от предыдущего рана (cleanup не сработал или проект удалили из БД, но не с сервера), новый деплой молча перезаписывает чужие файлы или конфликтует с запущенными контейнерами.

Аналогично: при `action=create` GitHub repo не должен существовать (уже пофикшено в `a0d0e7c`), а серверная директория — нет.

**Задачи**:
1. Прокинуть `action` из `EngineeringMessage` в `DeployMessage` (сейчас теряется — deploy не знает create vs feature vs fix)
2. В DevOps subgraph (или deploy-worker) перед dispatch `deploy.yml`:
   - `action=create`: SSH → проверить что `/opt/services/<NAME>/` **не существует** → если есть, fail fast с осмысленным сообщением
   - `action=feature`/`fix`: SSH → проверить что `/opt/services/<NAME>/` **существует** и контейнеры running → если нет, fail fast
3. Это заменяет хрупкую проверку `.env.bak` в `deploy.yml.jinja` (service-template) чистым сигналом от оркестратора

**Файлы**:
- `shared/contracts/queues/deploy.py` — добавить `action` field
- `services/langgraph/src/workers/engineering_worker.py:~913` — передать action в DeployMessage
- `services/langgraph/src/workers/deploy_worker.py` или DevOps subgraph нода — SSH pre-check

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
- **Worker dev-env port isolation**: `compose.base.yml` содержит `ports:` — при параллельных воркерах порты конфликтуют на хосте. Сейчас не проблема (mutex на project_id), но при параллелизации нужно: либо убрать `ports:` из base и вынести в `compose.dev.yml` с динамическими портами, либо compose runner должен инжектить уникальные порты через env.
- **"Добавить батарейку" к существующему проекту**: Механизм добавления модулей (backend, notifications и т.д.) в уже развёрнутый проект. Подход: агент получает инструкцию сходить в service-template, посмотреть структуру нужного модуля и переиспользовать код/паттерны самостоятельно. `copier update` для этого не годится — шаблон не поддерживает инкрементальное добавление модулей.

---

### 14. ~~Contract Consistency Improvements (Остаток #3+#5)~~ → ✅ Done
> Вызовы `redis.xadd` заменены на `client.publish_message()`, pydantic контракты внедрены.

---

## 💡 Ideas

### Self-hosted GitLab вместо GitHub
**Источник**: E2E Level C тесты (2026-03-02) — 50% failure rate на деплое из-за транзитных сетевых проблем GH Actions (Azure US) → Time4VPS (AS212531, Литва/Польша).
**Идея**: Заменить GitHub на self-hosted GitLab на Time4VPS. CI/CD runners, реестр образов и серверы деплоя — всё в одной AS212531. Полностью убирает трансатлантическую зависимость для CI/CD.
**Плюсы**: Нулевая сетевая латентность CI→deploy, предсказуемость, контроль над инфраструктурой.
**Минусы**: Maintenance overhead (обновления, бэкапы, HA), потеря GH ecosystem (Actions marketplace, Copilot integration, community visibility), миграция всех проектов.
**Промежуточный вариант**: Self-hosted GH Actions runner на VPS (минимальные изменения, тот же эффект для deploy).

---

## 🗑️ Completed / Superseded
*Архив или уже имплементированные решения*

- **Secrets Encryption**: Superseded by Fernet encryption in *Iter 1*.
- **Caddy Reverse Proxy**: Done in deploy-architecture *Iter 7*.
- **PO ReactAgent без контейнера**: Done. Переход на API-based ReactAgent завершен.
- **Dev Environment Docker-in-Docker Migration**: Фазы 1-4 завершены. (В планах осталось только E2E тестирование).
- **Redis Streams: PEL Recovery & Consumer Unification (#3+#5)**: Done. 9 consumer'ов переведены на `RedisStreamClient.consume()` с PEL recovery. Pydantic контракты на все очереди. См. [redis-streams-unification.md](plans/redis-streams-unification.md).
- **Pre-push Tests to Local venv (old #6)**: Done. Интегрирован быстрый локальный pytest скрипт без Docker overhead. Пункт #6 в бэклоге переиспользован для "Fix & Consolidate Test Suites".
- **Security Audit Base (#7)**: Done. Пароли отключены, deploy юзер создан, fail2ban/UFW настроены.
- **Contract Consistency (#14)**: Done. Избавились от сырых вызовов `xadd` в пользу методов клиента.
- **StrEnum Migration**: Done. 21 instance `(str, Enum)` → `StrEnum` в 14 файлах (shared/contracts, shared/models, shared/schemas, services/langgraph).
- **Stale ruff.toml Cleanup**: Done. Удалены 3 per-file-ignores для несуществующих файлов (`product_owner.py`, `capabilities/base.py`, `worker.py`).
- **MockProcess Test Dedup**: Done. Вынесен в `packages/worker-wrapper/tests/conftest.py`.
