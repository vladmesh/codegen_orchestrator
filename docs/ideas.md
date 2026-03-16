# Ideas

> [!WARNING]
> **DEPRECATED**: Этот файл более не является источником правды. Идеи мигрировали в базу данных (превращаются в `brainstorms` или `work_items`). Файл временно оставлен для генерации старого backlog.md.

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
- Prometheus migration path when server count > 10 — exporters (node_exporter + cadvisor) already installed, just add Prometheus central + switch from custom HTTP polling to Prometheus scraping (источник: brainstorm server-health-monitoring)
