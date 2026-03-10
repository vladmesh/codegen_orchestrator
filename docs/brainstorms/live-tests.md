# Live Tests — обзор идеи и план реализации

## Проблема

Текущая инфраструктура e2e-тестов устарела и не используется:

| Что есть сейчас | Проблема |
|---|---|
| `.claude/skills/e2e-run/SKILL.md` (1238 строк) | Огромный плейбук для Claude — 7 сценариев полного пайплайна. Устарел после рефакторинга (stories, tasks, architect, scaffolder как отдельный сервис) |
| `scripts/test_e2e_flow.py` | Элементарный CRUD-тест API (projects + servers). Не покрывает ничего реального |
| `scripts/e2e_scaffold_test.py` | Scaffold-only тест через worker-manager. Работает, но покрывает один кусок |
| `tests/e2e/test_live_smoke.py` | Worker spawn lifecycle. Адресует внутри docker-сети, не с хоста |
| `tests/e2e/test_infrastructure_sanity.py` | GitHub + Copier + Git проверка. Полезно, но изолированно |
| `tests/e2e/test_real_llm.py` | Тест с настоящим LLM. Дорого, медленно, ненадёжно |

**Ключевые проблемы:**
- Нет чёткого разделения между "дешёвыми" тестами (API + Redis + Docker) и "дорогими" (LLM)
- Плейбуки и скрипты дублируют друг друга
- Всё привязано к устаревшим контрактам
- Нет единого runner'а и отчётности

---

## Предложение: два набора тестов

### 1. `live-tests` — детерминированные тесты без LLM

Скрипты, которые запускаются **против живого стека** (`make up`). Всё что можно проверить без траты денег на LLM.

**Что тестируем:**

| Группа | Примеры проверок |
|---|---|
| **Health & Connectivity** | API `/health`, Redis ping, worker-manager health, consumer-группы в Redis |
| **API CRUD** | Projects, Tasks, Runs, Servers, Users, Stories — create/read/update/list |
| **Redis Streams** | Публикация сообщений в очереди (`engineering:queue`, `scaffold:queue`, `deploy:queue`, `po:input`), чтение ответов |
| **Worker Lifecycle** | Создание воркера через Redis → контейнер появляется → статус в Redis → удаление → cleanup |
| **Scaffold Pipeline** | Создание проекта → scaffold:queue → scaffolder подхватывает → GitHub repo заполнен → tree сохранён в DB |
| **Task Dispatcher** | Story с draft-проектом → dispatcher создаёт scaffold trigger → tasks создаются |
| **GitHub Integration** | GitHubAppClient auth → create repo → push → list files → delete repo |
| **Test Deploy** | Реальный деплой тестового проекта через оркестратор → получаем IP, порт, SSH-доступ → проверяем контейнеры на сервере |
| **Secrets & Crypto** | encrypt/decrypt roundtrip, env injection в worker |


**Чего НЕ тестируем:**
- LLM-ноды (architect, developer, PO, smoke_tester)
- Всё что стоит денег (API calls к Anthropic)

**Бонусы которые получим в процессе:**
- Выявим сервисы где не хватает логов (если нельзя понять прошёл ли тест без `docker logs`)
- Найдём слишком связанные куски (если нельзя протестировать scaffold без полного engineering-flow)
- Станет понятно, какие API endpoint'ы нужны для observable pipeline

### 2. `llm-playbooks` — обновлённые плейбуки с LLM

По сути — актуализированный `e2e-run`, разбитый на независимые сценарии:

| Плейбук | Что покрывает |
|---|---|
| **scaffold-only** | Project → Scaffold → GitHub (без LLM-worker) |
| **architect-only** | Scaffolded project → Architect → Tasks created |
| **single-task** | Один task → engineering:queue → worker → CI зелёный |
| **full-pipeline** | Draft project + story → scaffold → architect → tasks → engineering → deploy |
| **feature-add** | Готовый проект → feature request → engineering → deploy |
| **po-flow** | Через PO: natural language → project → scaffold → engineer → deploy |

---

## Структура в репозитории

```
tests/
├── live/                          # Набор 1: live-tests (детерминированные)
│   ├── conftest.py                # Общие фикстуры (api_client, redis_client, etc.)
│   ├── test_health.py             # Health checks всех сервисов
│   ├── test_api_crud.py           # Project/Task/Run/Server/User CRUD
│   ├── test_redis_streams.py      # Публикация и чтение из очередей
│   ├── test_worker_lifecycle.py   # Worker spawn → verify → delete
│   ├── test_scaffold_pipeline.py  # Полный scaffold flow
│   ├── test_task_dispatcher.py    # Dispatcher: trigger → create tasks
│   ├── test_github_integration.py # GitHubAppClient operations
│   ├── test_secrets.py            # Crypto roundtrip, secret injection
│   └── test_config_seed.py        # Seed data verification
│
├── playbooks/                     # Набор 2: llm-playbooks (дорогие)
│   ├── conftest.py                # Общие хелперы (poll_task, wait_for_deploy, etc.)
│   ├── test_scaffold_only.py
│   ├── test_architect_only.py
│   ├── test_single_task.py
│   ├── test_full_pipeline.py
│   ├── test_feature_add.py
│   └── test_po_flow.py
│
└── e2e/                           # Существующие (deprecated, удалим после миграции)
```

## Makefile targets

```makefile
# Live tests — quick, no LLM, run against `make up` stack
test-live:
	docker compose exec -T langgraph pytest /app/tests/live/ -v --tb=short

# Specific live test group
test-live-health:
	docker compose exec -T langgraph pytest /app/tests/live/test_health.py -v

test-live-worker:
	docker compose exec -T langgraph pytest /app/tests/live/test_worker_lifecycle.py -v

# LLM playbooks — expensive, manual
test-playbook PLAYBOOK=full_pipeline:
	docker compose exec -T langgraph pytest /app/tests/playbooks/test_$(PLAYBOOK).py -v -s --timeout=3600

# Run all playbooks sequentially
test-playbooks:
	docker compose exec -T langgraph pytest /app/tests/playbooks/ -v -s --timeout=7200
```

## Окружение

| Параметр | Live Tests | LLM Playbooks |
|---|---|---|
| **Стек** | `make up` (dev) | `make up` (dev) |
| **LLM** | Не нужен | `ANTHROPIC_API_KEY` обязателен |
| **GitHub** | `GITHUB_APP_*` (для scaffold/github тестов) | `GITHUB_APP_*` обязателен |
| **Серверы** | Опционально (skip если нет) | Нужен managed-сервер для deploy |
| **Telegram** | Не нужен | Опционально (для PO flow) |
| **CI** | ❌ Не в CI | ❌ Не в CI |
| **Время** | 2-5 мин | 10-60 мин на плейбук |

---

## План реализации

### Фаза 1: Фундамент (live-tests)
1. Создать `tests/live/conftest.py` с общими фикстурами
2. Мигрировать `test_live_smoke.py` → `tests/live/test_health.py` + `test_worker_lifecycle.py`
3. Мигрировать `test_e2e_flow.py` → `tests/live/test_api_crud.py`
4. Добавить `test_redis_streams.py` — pub/sub проверки для каждой очереди
5. Добавить Makefile targets (`test-live`, `test-live-*`)

### Фаза 2: Scaffold и Dispatcher
6. Мигрировать `e2e_scaffold_test.py` → `tests/live/test_scaffold_pipeline.py`
7. Добавить `test_task_dispatcher.py` — проверка что dispatcher подхватывает draft stories
8. Добавить `test_github_integration.py`

### Фаза 3: Playbooks
9. Написать `tests/playbooks/conftest.py` с хелперами
10. Разбить `SKILL.md` на отдельные test-файлы
11. Адаптировать под текущие контракты (stories → tasks → runs)

### Фаза 4: Cleanup
12. Удалить `tests/e2e/` (deprecated)
13. Удалить/архивировать `.claude/skills/e2e-run/`
14. Обновить `docs/TESTING.md`

---

## Побочные эффекты (ради чего тоже стоит делать)

- **Обнаружение недостаточного логирования** — если live-тест не может верифицировать результат без `docker logs grep`, значит сервису нужны API endpoint'ы или structured-логи
- **Обнаружение чрезмерной связанности** — если нельзя протестировать scaffold отдельно от engineering, это сигнал к декаплингу
- **Living documentation** — тесты как документация текущего API и потоков данных
- **Быстрый feedback loop** — `make test-live` за 2-5 минут перед пушем сложных изменений
