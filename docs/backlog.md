# Backlog

> [!WARNING]
> **DEPRECATED**: Этот файл более не является источником правды. Задачи мигрировали в базу данных (таблица `tasks`). Этот файл автогенерируется скриптом исключительно для read-only просмотра.

> **Актуально на**: 2026-03-07 (generated)

## Queue (ordered by priority, first = next)

### #52 Scaffold script не экранирует task_description
- **Priority**: MEDIUM
- **Plan**: —
- **Status**: backlog
- **Brief**: `manager.py:819` подставляет `scaffold_config.task_description` напрямую в bash f-string: `--data "task_description={scaffold_config.task_description}"`. Описание задачи содержит многострочный текст с двойными кавычками, скобками, спецсимволами bash. При интерполяции в f-string двойные кавычки из...

### #21 Deploy Pre-Check
- **Priority**: MEDIUM
- **Plan**: —
- **Status**: backlog
- **Brief**: Валидация сервера перед деплоем. Прокинуть `action` (create/feature/fix) в DeployMessage. SSH-проверка `/opt/services/<NAME>/`. Файлы: `shared/contracts/queues/deploy.py`, `engineering_worker.py`, `deploy_worker.py`.

### Repository model + migration
- **Priority**: MEDIUM
- **Plan**: —
- **Status**: backlog
- **Brief**: Новая сущность Repository (id, project_id, name, git_url, provider_repo_id, role, is_managed). Alembic миграция + CRUD API. Миграция существующих Project.repository_url → Repository(role=primary). Task.repository_id nullable FK.

### Story model + API
- **Priority**: MEDIUM
- **Plan**: —
- **Status**: backlog
- **Brief**: Новая сущность Story (id, project_id, parent_story_id, title, description, acceptance_criteria, status, created_by). Alembic миграция + CRUD API + action-based status transitions. Task.story_id FK. parent_story_id — self-ref FK для epic-like группировки.

### #18 Split engineering_worker.py (1088 LOC)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Вынести фазы (scaffold, CI fix loop, deploy trigger) в отдельные модули.

### #7 Security Audit: Deploy Cleanup
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Очистка зависших контейнеров/образов после деплоев (`docker image prune`). SSH hardening уже done в ansible. Priority adjusted by triage (roadmap phase change).

### #10 Worker Lifecycle (Pause/Unpause)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: `docker pause` при бездействии. CPU/RAM лимиты на контейнеры.

### #54 Deploy: inter-service URL должен использовать docker service name
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: DevOps-ноды генерируют `.env` на сервере с `BACKEND_API_URL=http://<external_ip>:8000`. Сервисы внутри одного compose-стека (например, tg_bot → backend) ходят через внешний IP вместо docker DNS (`http://backend:8000`). Это хрупко: зависит от внешней сети, обходит docker networking, ломается при f...

### #62 /brainstorm resume — продолжение обсуждения существующего драфта
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: /brainstorm должен уметь подхватить существующий draft из БД и продолжить дискуссию. Сценарий: /brainstorm resume → GET /api/brainstorms/?status=draft → список → выбор → дополнение content. Также: миграция 14 legacy brainstorms из docs/brainstorms/ в БД (status=draft/done/triaged по текущему стат...

### #59 PO work item tools (Step 4)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Новые PO tools: create_work_item, list_work_items, get_work_item, start_work_item. start_work_item внутри вызывает trigger_engineering (старый механизм). PO промпт обновляется: мыслить фичами, не engineering tasks. К этому моменту API стабилен и проверен на десятках задач dogfooding. Источник: br...

### #60 Engineering worker work_item lifecycle (Step 5)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: engineering_worker при наличии work_item_id: пишет iteration_start/iteration_end events, CI fix attempts → events с деталями, обновляет work_item.status (in_dev → testing → done). Deploy worker обновляет status при успешном деплое. Полный audit trail: сколько итераций, что фейлилось, почему. Reop...

### #2 Agent Hierarchy & Incident Response
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: TaskAssessor, Watchdog & Recovery (DockerEventsListener, DLQ consumer), shared session memory ("предсмертная записка" агента). Brainstorm: `docs/brainstorms/agent-hierarchy.md`. Priority adjusted by triage (roadmap phase change). NB: Watchdog/DLQ scope уменьшится — WorkItemEvent (#55) покрывает a...

### #19 Split github.py Client (986 LOC)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Разбить на submodules по domain: repos, actions, secrets, workflows. Фасад делегирует в sub-clients.

### #20 API Key & SSH Key Encryption
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Применить SecretsCipher (Fernet) к API key values и SSH keys. TODO-комменты в `api_keys.py:36,72` и `servers.py:66`.

### #11 E2E Tests Completion
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Завершить покрытие E2E (Level 5-7). Добавить E2E mock-тесты (Level A+B) в CI.

### #26 Notifications via Redis Stream (убрать прямую зависимость от Telegram API)
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Сейчас `shared/notifications.py` шлёт в Telegram API напрямую — scheduler, infra-service держат `TELEGRAM_BOT_TOKEN`. Нужно: сервисы публикуют в Redis stream `notifications:queue`, telegram_bot потребляет и отправляет. Убирает `TELEGRAM_BOT_TOKEN` из всех сервисов кроме telegram_bot, упрощает тес...

### #41 Parallel Server Provisioning
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: infra-service обрабатывает `provisioner:queue` последовательно — один consumer loop с `await` на каждый job (`services/infra-service/src/main.py:127-148`). При 3+ серваках в `PENDING_SETUP` каждый Ansible прогон (~15 мин) блокирует очередь. LangGraph-сторона уже параллельна (`asyncio.create_task`...

### #46 Rename duckduckgo_search → ddgs
- **Priority**: LOW
- **Plan**: —
- **Status**: backlog
- **Brief**: Пакет `duckduckgo_search` переименован в `ddgs`. Runtime warning в логах: `This package has been renamed to ddgs! Use pip install ddgs instead.` Заменить зависимость в `services/langgraph/pyproject.toml`, обновить импорт в `services/langgraph/src/po/tools.py`, перегенерировать lock-файл (`make lo...


## Done (last 10)

- #999 Smoke test task — 2026-03-07
- make sync — генерация docs из БД (backlog, roadmap, status, recent plans/brainstorms) — 2026-03-07
- #64 Implement skill: PR flow + in_ci status + need_e2e — 2026-03-07
- Rename WorkItem→Task, Task→Run — 2026-03-07
- #63 Milestone model + ROADMAP generation — 2026-03-07
- #61 Brainstorm model in DB — 2026-03-07
- #58 Skills → API + Simplified Model — 2026-03-07
- #57 /implement work item events (Step 2) — 2026-03-07
- #56 /next skill via API (Step 1) — 2026-03-07
- #8 Workspace Failure Counter — 2026-03-07

## Ideas

Manually maintained list of ideas and future improvements.
Read by `make backlog` to include in generated backlog.md.

- Project Name Collision: repo_name и deploy path строятся из `project.name`, а не `project.id` — два юзера с одинаковым именем получают один GitHub-репо, один deploy path `/opt/services/{name}/`, один Docker-образ. Фикс: включить `project_id` в repo name (`my-bot-a1b2c3d4`). Затронуты: `engineering_worker.py:517-519` (repo name gen), `github.py:940` (create_repo), `devops/nodes.py:443,348` (PROJECT_NAME secret). Post-MVP. (источник: анализ #30 multi-user isolation)

- Self-hosted GitLab или GH runner на VPS (источник: E2E failure rate 50%, 2026-03-02)
- Admin UI: projects, workers, logs (источник: MVP Phase 4)
- Tester node (полный): QA-агент с Claude + Playwright после деплоя (источник: brainstorm qa-node.md, post-MVP)
- CI Monitor Node: вынести `_wait_for_ci_and_fix` в LangGraph-ноду (источник: audit)
- API Authentication: заменить `x-telegram-id` на JWT (источник: audit)
- Telegram Bot Pool: пре-зарегистрированные боты (источник: US2)
- Cost Tracking: LLM токены per user/project (источник: roadmap Phase 6)
- Deploy Rollback: откат при failed health checks (источник: audit)
- Docker Python SDK для worker-manager (источник: audit-v2)
- Fix `sys.path` hack в telegram_bot (источник: audit)
- Split Tier 2 large files: devops/nodes.py, telegram_bot/main.py, env_analyzer.py (источник: audit-v2)
- Worker port isolation: убрать `ports:` из compose.base.yml при параллелизации (источник: audit)
- Enable Ruff S110 + BLE001 rules to catch swallowed/broad exceptions (источник: audit 2026-03-04)
- pytest-xdist для backend integration tests — исследовать после параллелизации стеков (источник: brainstorm ci-integration-test-speed)
- Split worker-manager/src/manager.py (828 LOC, 6 functions >50 LOC) (источник: audit 2026-03-05)
- infra-service unit test coverage: 9 source files, 0 tests (источник: audit 2026-03-05)
- ~~Task Store в БД~~ — поглощено #55 (WorkItem Model + API)
- ~~Миграция скиллов на API-first~~ — станет #56-58 (Steps 1-3 из brainstorm orchestrator-v2-task-management)
- Assessor node — фильтр сложности запросов, теперь на базе WorkItem (Phase 4) (источник: brainstorm epic-decomposition)
- Architect node — декомпозиция сложных задач на WorkItems (Phase 5) (источник: brainstorm epic-decomposition)
- SOPS для .env на проде (Phase 2B) (источник: brainstorm epic-decomposition)
- Zero-downtime deploy — rolling restart (Phase 2B) (источник: brainstorm epic-decomposition)
- RLS policies на PostgreSQL для multi-tenant (подготовка, не блокер для MVP) (источник: brainstorm multi-tenant-isolation)
- Redis key prefix isolation (tenant:{id}:*) — подготовка к multi-tenant (источник: brainstorm multi-tenant-isolation)
- Отдельная database для системных данных оркестратора (orchestrator_system) — Phase 3 (источник: brainstorm multi-tenant-isolation)
- Унифицировать Time4VPS credentials: infra-service читает из env vars, scheduler — из api_keys таблицы через API. Один источник правды. (источник: seed/nuke audit 2026-03-05)
- Shared Docker image layer для интеграционных тестов — собрать api/db/redis один раз, шарить между стеками через GHA artifacts (источник: brainstorm ci-integration-test-speed, Option B)
- Объединить мелкие compose-стеки (frontend 1 тест + infra 2 теста) для экономии одного up/down цикла (источник: brainstorm ci-integration-test-speed, Option D)
- CI: cache copier template clone для template integration tests — marginal gain ~10-15с, сложный cache invalidation (источник: brainstorm ci-integration-test-speed)
- Отдельный UI/UX для подтверждения собранного ТЗ пользователем перед инженерным этапом (источник: brainstorm po-smart-node)
- Functional health check: текущий healthcheck проверяет только что процесс жив (HTTP 200 на /health). Не ловит ситуации когда таблицы не созданы, миграции не прошли, seed-данные отсутствуют — бэкенд "healthy", но 500 на каждый бизнес-запрос. Можно добавить в service-template readiness probe с `SELECT 1` или проверкой ключевых таблиц (источник: fortune-telling-bot — backend healthy, но relation "fortunes" does not exist)
