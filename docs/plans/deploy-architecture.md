# Plan: Новая архитектура деплоя

> **Дата**: 2026-02-16
> **Ветка**: `feat/deploy-architecture`
> **Контекст**: E2E тест выявил что Ansible-деплой не работает (билд на VPS падает, пароли рассогласованы, health check маскирует ошибки). Переходим на GitHub Actions deploy + Fernet-шифрование секретов + env resolver pipeline.
> **Источник**: `docs/brainstorms/deploy-architecture.md`

---

## Scope

**Что меняется в оркестраторе:**
1. Fernet encryption для `project.config.secrets`
2. Env resolver с группами связанных переменных (PostgresGroup и т.д.)
3. DeployerNode: вместо Ansible delegation → собирает DOTENV, пишет GitHub Secrets, тригерит `deploy.yml`, ждёт результат
4. Удаление deploy-кода из infra-service (provisioning остаётся)
5. Удаление `ansible:deploy:queue`, `delegate_ansible_deploy`, `DeploymentJobRequest/Result`
6. Добавление `deployed_sha` в `ServiceDeployment`

**Что меняется в service-template (отдельный PR, не в этом плане):**
- Разделение `main.yml.jinja` → `ci.yml.jinja` + `deploy.yml.jinja`
- Профили только в `compose.dev.yml`

**Что НЕ входит:**
- Multi-project resource limits / мониторинг (вопрос 7 — отложен)
- Автоматический rollback и откат миграций
- Reverse proxy (Caddy/Traefik) — когда понадобятся домены

---

## Iteration 0: Подготовка

### 0.1 Создать ветку
```bash
git checkout -b feat/deploy-architecture
```

### 0.2 Snapshot текущих тестов
```bash
make test-unit
make test-langgraph-unit
```
Зафиксировать количество passing тестов — все должны оставаться зелёными после каждой итерации.

---

## Iteration 1: Fernet encryption для секретов ✅

> **Статус**: Done (2026-02-16)
> **Детальный план реализации**: был в промпте к Claude Code, включал TDD порядок, точки интеграции, mock-стратегию

**Цель:** Секреты в `project.config.secrets` зашифрованы at rest. Читающий/пишущий код работает через единый интерфейс.

### 1.1 Написать `shared/crypto.py`
- Класс `SecretsCipher` с методами `encrypt(plaintext: str) -> str` и `decrypt(ciphertext: str) -> str`
- Использует `cryptography.fernet.Fernet`
- Ключ из env var `SECRETS_ENCRYPTION_KEY`
- Если ключ не задан → `RuntimeError` (fail fast, без дефолтов)
- Функция `encrypt_dict(d: dict) -> dict` — шифрует все values
- Функция `decrypt_dict(d: dict) -> dict` — дешифрует все values
- Обработка невалидных/незашифрованных значений: `decrypt` ловит `InvalidToken`, логирует warning, возвращает raw value (для обратной совместимости при миграции)

### 1.2 Тесты для `shared/crypto.py`
- `test_encrypt_decrypt_roundtrip`
- `test_encrypt_dict_decrypt_dict_roundtrip`
- `test_different_values_different_ciphertexts`
- `test_missing_key_raises_runtime_error`
- `test_decrypt_plaintext_value_returns_as_is` (graceful degradation)

### 1.3 Интегрировать в SecretResolverNode
Файл: `services/langgraph/src/subgraphs/devops/nodes.py`
- `_save_secrets_to_project()`: шифровать перед записью (`encrypt_dict`)
- При чтении `config_secrets`: дешифровать (`decrypt_dict`)

### 1.4 Интегрировать в PO tools (set_project_secret)
Файл: `services/langgraph/src/po/tools.py`
- `set_project_secret()` (строки ~147-149): читает `config.secrets`, добавляет новый секрет, пишет обратно
- Decrypt перед чтением, encrypt перед записью

### 1.5 Интегрировать в orchestrator-cli (set_secret_async)
Файл: `packages/orchestrator-cli/src/orchestrator_cli/commands/project.py`
- `set_secret_async()` (строки ~101-104): аналогичный паттерн — read → update → write
- Decrypt перед чтением, encrypt перед записью

### 1.6 Обновить тесты SecretResolverNode
Файл: `services/langgraph/tests/unit/test_secret_resolver.py`
- Мокать `SecretsCipher` — тесты не должны зависеть от реального ключа
- Добавить тест: зашифрованные секреты корректно дешифруются при повторном резолве

### 1.7 Добавить `SECRETS_ENCRYPTION_KEY` в docker-compose
- `docker-compose.yml`: передать env var в langgraph и deploy-worker сервисы
- `.env.example`: добавить `SECRETS_ENCRYPTION_KEY=` с комментарием как сгенерировать (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)

### 1.8 Добавить `cryptography` в зависимости
- `shared/pyproject.toml` (секция `dependencies`)
- `make lock-deps`

### E2E проверка итерации 1
```bash
make test-unit  # все тесты зелёные
make test-langgraph-unit  # все тесты зелёные
```
Ручная проверка:
1. `make up`
2. Создать проект через Telegram, дождаться scaffolding
3. Тригернуть деплой
4. Проверить в БД: `SELECT config->'secrets' FROM projects WHERE id = '...'` — значения должны быть зашифрованными строками (Fernet token начинается с `gAAAAA...`)
5. В логах langgraph: `secret_reused` события должны работать (дешифровка успешна)

---

## Iteration 2: Env Resolver с группами ✅

> **Статус**: Done (2026-02-16)
> **Scope**: пункты 2.1–2.4 реализованы. Пункты 2.5–2.6 (парсинг комментариев .env.example, контекст compose для LLM) отложены — независимы от групп.

**Цель:** Связанные переменные (POSTGRES_PASSWORD, DATABASE_URL, ASYNC_DATABASE_URL) генерируются согласованно через группы. LLM fallback только для неизвестных переменных.

### 2.1 Создать `services/langgraph/src/subgraphs/devops/env_groups.py`

Базовый класс и реализации групп:

```python
class EnvGroup(ABC):
    """Группа связанных env-переменных."""
    @abstractmethod
    def handles(self, variables: set[str]) -> set[str]:
        """Какие переменные из входного набора эта группа обрабатывает."""

    @abstractmethod
    def resolve(self, project_id: str, variables: set[str]) -> dict[str, str]:
        """Сгенерировать согласованный набор значений."""
```

Группы:
- **PostgresGroup**: обрабатывает `{DATABASE_URL, ASYNC_DATABASE_URL, POSTGRES_PASSWORD, POSTGRES_USER, POSTGRES_DB}`. Генерирует пароль один раз, строит все URL из него.
- **RedisGroup**: обрабатывает `{REDIS_URL}`. Возвращает `redis://redis:6379/0`.

<!-- Опция: в будущем можно добавить MongoGroup, RabbitMQGroup по аналогии -->

Функция `resolve_with_groups(variables: set[str], project_id: str) -> tuple[dict, set]`:
- Пропускает через все группы → собирает resolved + consumed
- Возвращает (resolved_values, remaining_variables)

### 2.2 Тесты для env_groups
- `test_postgres_group_generates_consistent_password` — пароль в DATABASE_URL совпадает с POSTGRES_PASSWORD
- `test_postgres_group_handles_subset` — если есть только DATABASE_URL без POSTGRES_PASSWORD, всё равно генерит согласованно
- `test_postgres_group_async_url` — ASYNC_DATABASE_URL использует `postgresql+asyncpg://`
- `test_redis_group`
- `test_resolve_with_groups_returns_remaining` — переменные не обработанные группами возвращаются как remaining
- `test_groups_do_not_overlap` — ни одна переменная не обрабатывается двумя группами

### 2.3 Рефакторинг SecretResolverNode

Файл: `services/langgraph/src/subgraphs/devops/nodes.py`

Текущий `_generate_infra_secret()` заменяется на pipeline:
1. Собрать infra-переменные из `env_analysis`
2. `resolve_with_groups(infra_vars, project_id)` → grouped_values, remaining
3. Для remaining: текущая логика `_generate_infra_secret()` (random tokens, etc.)
4. Merge всё в `resolved`

`_generate_infra_secret()` остаётся как fallback для переменных не попавших в группы, но `DATABASE_URL` и `POSTGRES_PASSWORD` из него убираются (теперь это PostgresGroup).

### 2.4 Обновить тесты SecretResolverNode
- Удалить/обновить тесты которые проверяли независимую генерацию DATABASE_URL и POSTGRES_PASSWORD
- Добавить интеграционный тест: полный pipeline с `env_analysis = {"DATABASE_URL": "infra", "POSTGRES_PASSWORD": "infra", ...}` → проверить что пароли совпадают

### 2.5 Контекст для LLM fallback (env_analyzer)

Файл: `services/langgraph/src/subgraphs/devops/env_analyzer.py`

Расширить контекст для LLM при классификации неизвестных переменных:
1. Комментарии из `.env.example` — парсить строку над переменной
2. Compose файлы: fetch `infra/compose.base.yml` через GitHub API, извлечь `environment:` секции

<!-- Опция: code search через GitHub Search API (`os.getenv("VAR_NAME")`) — можно добавить позже если LLM-классификация будет ошибаться -->

Обновить `_parse_env_variables()` → возвращать `list[tuple[str, str | None]]` (name, comment).

### 2.6 Обновить тесты env_analyzer
- Тест парсинга комментариев из `.env.example`
- Тест что контекст из compose включается в LLM prompt

### E2E проверка итерации 2
```bash
make test-unit  # все тесты зелёные (272 passed)
make test-langgraph-unit  # все тесты зелёные (94 passed: 81 existing + 9 env_groups + 4 integration)
make lint  # без ошибок
```
Ручная проверка на живом стеке (выполнена):
1. First deploy: DATABASE_URL, ASYNC_DATABASE_URL, POSTGRES_PASSWORD — один пароль (когерентно)
2. Redeploy (partial cache): cached secrets сохраняются, uncached генерируются
3. Mixed scenario (infra+computed+user): группы + fallback + computed + missing_user — всё корректно
4. В логах: `secrets_grouped count=N vars=[...]` для групповых, `secret_generated` для fallback

---

## Iteration 3: DeployerNode → GitHub Actions ✅

> **Статус**: Done (2026-02-16)

**Цель:** Заменить Ansible delegation на: собрать DOTENV → записать GitHub Secrets → тригернуть deploy.yml → ждать результат.

### 3.1 Новый метод в GitHubAppClient: `trigger_workflow_dispatch`

Файл: `shared/clients/github.py`

```python
async def trigger_workflow_dispatch(
    self, owner: str, repo: str, workflow_file: str, ref: str = "main", inputs: dict | None = None
) -> bool:
```
- `POST /repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches`
- Возвращает True если 204

### 3.2 Тесты для `trigger_workflow_dispatch`
- Мок HTTP: 204 → True
- Мок HTTP: 404 → raise
- Мок HTTP: 422 → raise (workflow не существует)

### 3.3 Утилита сборки DOTENV

Файл: `services/langgraph/src/subgraphs/devops/dotenv_builder.py`

```python
def build_dotenv(secrets: dict[str, str]) -> str:
    """Собрать .env контент из dict."""

def encode_dotenv(dotenv_content: str) -> str:
    """Base64 encode .env content."""
```

### 3.4 Тесты для dotenv_builder
- `test_build_dotenv_format` — `KEY=VALUE\n` формат
- `test_build_dotenv_escaping` — значения с `=`, пробелами, кавычками
- `test_encode_dotenv_base64_roundtrip`
- `test_dotenv_size_under_48kb` — предупреждение если base64 > 48KB (лимит GitHub Secrets)

### 3.5 Переписать DeployerNode

Файл: `services/langgraph/src/subgraphs/devops/nodes.py`

Новая логика `DeployerNode.run()`:
1. Собрать DOTENV из `resolved_secrets` (через `dotenv_builder`)
2. Записать GitHub Secrets:
   - `DOTENV` — base64-encoded .env
   - `DEPLOY_HOST` — server_ip из allocated_resources
   - `DEPLOY_USER` — "root"
   - `DEPLOY_SSH_KEY` — SSH key (из mounted volume)
   - `DEPLOY_PORT` — allocated port
   - `PROJECT_NAME` — project name
3. Тригернуть `deploy.yml` через `trigger_workflow_dispatch`
4. Ждать completion через `wait_for_workflow_completion` (workflow_file=`deploy.yml`, таймаут 10 мин)
5. На успех: создать `ServiceDeployment` запись, обновить статус → `active`
6. На failure: получить логи через `get_workflow_failure_logs`, обновить статус → `error`

**Убрать:** импорт и вызов `delegate_ansible_deploy`.

### 3.6 Обновить `_setup_ci_secrets()`

Переименовать в `_write_deploy_secrets()`. Расширить набор:
- Добавить `DOTENV`, `DEPLOY_PORT`, `PROJECT_NAME`
- Убрать `DEPLOY_PROJECT_PATH`, `DEPLOY_COMPOSE_FILES` (workflow сам знает пути)

### 3.7 Добавить `deployed_sha` в ServiceDeployment

Файл: `shared/models/service_deployment.py`
```python
deployed_sha: Mapped[str | None] = mapped_column(nullable=True)
```

Миграция:
```bash
make makemigrations MSG='add deployed_sha to service_deployments'
```

DeployerNode: при создании deployment record заполнять `deployed_sha` из `run_info["head_sha"]` (ответ GitHub API workflow run).

### 3.8 Тесты DeployerNode (полный переписать)

Файл: `services/langgraph/tests/unit/test_deployer.py` (новый)

Мокать: `GitHubAppClient`, `api_client`, `SecretsCipher`

- `test_deployer_writes_dotenv_secret` — проверить что `set_repository_secrets` вызван с `DOTENV`
- `test_deployer_triggers_workflow_dispatch` — проверить вызов `trigger_workflow_dispatch("deploy.yml")`
- `test_deployer_waits_for_completion` — мок `wait_for_workflow_completion` → success
- `test_deployer_handles_workflow_failure` — мок failed workflow → error в state
- `test_deployer_handles_ci_timeout` — мок TimeoutError
- `test_deployer_creates_deployment_record_with_sha`
- `test_deployer_updates_project_status_to_active_on_success`
- `test_deployer_updates_project_status_to_error_on_failure`

### 3.9 Удалить delegation код

- Удалить `services/langgraph/src/tools/devops_delegation.py`
- Удалить `shared/schemas/deployment_jobs.py`
- Убрать импорты из `nodes.py`
- Удалить `services/langgraph/tests/service/test_deploy_flow.py` (тестировал старый polling)

### E2E проверка итерации 3

```bash
make test-langgraph-unit  # 108 passed
make test-shared-unit     # 61 passed
make lint                 # All checks passed
```

E2E на живом стеке (выполнено):
1. API roundtrip: POST/GET/PATCH/DELETE `deployed_sha` — корректно
2. DB: колонка `deployed_sha` (varchar, nullable) существует
3. `dotenv_builder` в langgraph контейнере: build → encode → decode roundtrip
4. `trigger_workflow_dispatch` и `head_sha` — сигнатуры и код присутствуют
5. DeployerNode импортируется, старый код (`delegate_ansible_deploy`, `DeploymentJobRequest`) удалён
6. Полный pipeline на GitHub: создан `deploy-e2e-test` в `project-factory-test`, записаны 6 секретов, dispatch `deploy.yml`, workflow completed (success), `head_sha` получен. Репо удалён после теста.

Документация обновлена: `docs/NODES.md`, `docs/CONTRACTS.md`, `CLAUDE.md`, `docs/STATUS.md`.

---

## Iteration 4: Очистка infra-service от deploy-кода

**Цель:** infra-service обслуживает только provisioning. Deploy-код и queue удаляются.

### 4.1 Удалить deployer из infra-service

- Удалить `services/infra-service/src/deployer/` (весь каталог)
- Из `services/infra-service/src/main.py`:
  - Убрать импорт `deploy_project`
  - Убрать `process_deploy_job()`
  - Убрать `ANSIBLE_DEPLOY_QUEUE` из `xreadgroup` streams
  - Оставить только `PROVISIONER_QUEUE`

### 4.2 Удалить `ansible:deploy:queue` из topology

Файл: `shared/queues.py`
- Удалить `ANSIBLE_DEPLOY_QUEUE = "ansible:deploy:queue"`
- Удалить `QueueBinding(ANSIBLE_DEPLOY_QUEUE, INFRA_GROUP, "Ansible deployments")`

### 4.3 Обновить тесты

- Убрать тесты связанные с ansible deploy queue если есть
- Проверить что оставшиеся тесты infra-service проходят

### 4.4 Обновить документацию

> Уже выполнено в Iteration 3: `docs/NODES.md`, `docs/CONTRACTS.md`, `CLAUDE.md` обновлены.

### E2E проверка итерации 4
```bash
make test-unit  # все тесты зелёные
make test-langgraph-unit
```
Ручная проверка:
1. `make up` — infra-service стартует без ошибок
2. В логах infra-service: `infrastructure_worker_started`, слушает только `provisioner:queue`
3. Полный E2E: создание проекта → scaffolding → engineering → deploy → проверка на сервере

---

## Iteration 5: Feature deploy flow

**Цель:** При пуше в main после CI → оркестратор проверяет новые env vars → обновляет DOTENV → тригерит deploy.yml.

### 5.1 Research: механизм обнаружения push

Исследовать как оркестратор узнаёт о пуше:
- Вариант A: GitHub webhook → endpoint в API → кидает в deploy:queue
- Вариант B: Polling через scheduler (проверять latest commit SHA)
- Вариант C: CI workflow сам нотифицирует (workflow_dispatch callback)

<!-- Рекомендация: webhook (вариант A) — самый надёжный и real-time. Но требует публичный endpoint. Для MVP можно polling (B). -->

Результат research определит реализацию 5.2-5.4.

### 5.2 Реализовать обнаружение

По результатам 5.1.

### 5.3 Env diff логика

Новая функция (в env_analyzer или отдельный модуль):
```python
async def check_env_changes(project_id: str, owner: str, repo: str) -> set[str]:
    """Fetch .env.example, сравнить keys с БД, вернуть new_vars."""
```
- `keys_in_example - keys_in_db = new_vars`
- Если `new_vars` пустой → DOTENV не изменился, можно деплоить
- Если есть новые → запустить env resolver только для них

### 5.4 Обновить deploy worker

При получении feature-deploy задачи:
1. Fetch `.env.example` → compare keys → resolve new vars
2. Rebuild DOTENV → update GitHub Secret
3. Trigger deploy.yml → wait → update status

### 5.5 Тесты
- `test_check_env_changes_no_new_vars`
- `test_check_env_changes_with_new_vars`
- `test_feature_deploy_updates_dotenv`
- `test_feature_deploy_skips_dotenv_when_no_changes`

### E2E проверка итерации 5
```bash
make test-unit
make test-langgraph-unit
```
Ручная проверка:
1. Задеплоенный проект — пушнуть изменение в main (без новых env vars)
2. **Ожидаемые логи:**
   - `check_env_changes new_vars=0`
   - `workflow_dispatch_triggered workflow_file=deploy.yml`
   - `workflow_completed conclusion=success`
3. Пушнуть изменение с новой переменной в `.env.example`
4. **Ожидаемые логи:**
   - `check_env_changes new_vars=1 vars=['NEW_VAR']`
   - `env_resolver_start` (только для NEW_VAR)
   - `dotenv_updated`
   - `workflow_dispatch_triggered`

---

## Iteration 6: Финальный E2E и мёрж

### 6.1 Полный E2E тест: первый деплой

Сценарий: новый проект от начала до конца.

1. Отправить описание проекта в Telegram
2. PO создаёт проект, запрашивает TELEGRAM_BOT_TOKEN
3. Пользователь отвечает токеном
4. Scaffolding → engineering → deploy
5. **Проверки:**
   - GitHub repo → Actions → `ci.yml` зелёный, образы в GHCR с SHA-тегом
   - GitHub repo → Actions → `deploy.yml` зелёный
   - SSH на сервер → `docker compose ps` → все контейнеры running
   - `.env` на сервере содержит все переменные
   - Пароль в DATABASE_URL совпадает с POSTGRES_PASSWORD
   - БД оркестратора: проект status=active, service_deployment record с deployed_sha
   - БД оркестратора: `project.config.secrets` — зашифрованы (начинаются с `gAAAAA`)

### 6.2 Полный E2E тест: feature deploy

Сценарий: пуш изменения в задеплоенный проект.

1. Изменить код в репо, пушнуть в main
2. CI проходит, образы обновляются
3. Оркестратор обнаруживает → тригерит deploy.yml
4. **Проверки:**
   - Новые контейнеры используют свежие образы
   - `.env` не изменился (если нет новых переменных)
   - `deployed_sha` обновился в БД

### 6.3 E2E тест: feature deploy с новыми переменными

1. Добавить новую переменную в `.env.example`, пушнуть
2. **Проверки:**
   - Env resolver обработал новую переменную
   - DOTENV обновлён в GitHub Secrets
   - `.env` на сервере содержит новую переменную
   - Контейнеры работают

### 6.4 E2E тест: missing user secrets

1. Добавить `STRIPE_API_KEY` в `.env.example`, пушнуть
2. **Проверки:**
   - PO спрашивает пользователя через Telegram
   - До ответа — деплой не тригерится
   - После ответа — деплой проходит

### 6.5 E2E тест: deploy failure

1. Сломать что-то (невалидный Docker image, порт занят)
2. **Проверки:**
   - deploy.yml фейлится
   - Оркестратор видит failure
   - `project.status = error`
   - PO нотифицирует пользователя

### 6.6 Обновить документацию

- `docs/brainstorms/deploy-architecture.md` — пометить как реализованный
- `docs/NODES.md` — финальное состояние DevOps subgraph
- `docs/CONTRACTS.md` — актуальная топология очередей
- `CLAUDE.md` — актуальный architecture diagram

### 6.7 Code review и мёрж

```bash
make test-unit
make test-langgraph-unit
make test-all  # если есть integration тесты
make lint
```

```bash
git push -u origin feat/deploy-architecture
gh pr create --title "feat: deploy via GitHub Actions, Fernet secrets, env groups"
```

После ревью и зелёного CI:
```bash
gh pr merge
```

---

## Файлы (сводка)

### Новые файлы
| Файл | Итерация |
|------|----------|
| `shared/crypto.py` | 1 |
| `shared/tests/unit/test_crypto.py` | 1 |
| `services/langgraph/src/subgraphs/devops/env_groups.py` | 2 |
| `services/langgraph/tests/unit/test_env_groups.py` | 2 |
| `services/langgraph/src/subgraphs/devops/dotenv_builder.py` | 3 |
| `services/langgraph/tests/unit/test_dotenv_builder.py` | 3 |
| `services/langgraph/tests/unit/test_deployer.py` | 3 |
| `services/api/migrations/versions/5bc39eece23f_...` | 3 |

### Изменяемые файлы
| Файл | Итерация | Что меняется |
|------|----------|--------------|
| `services/langgraph/src/subgraphs/devops/nodes.py` | 1, 2, 3 | Encryption, groups, GH Actions deploy |
| `services/langgraph/src/po/tools.py` | 1 | Encrypt/decrypt в `set_project_secret()` |
| `packages/orchestrator-cli/src/orchestrator_cli/commands/project.py` | 1 | Encrypt/decrypt в `set_secret_async()` |
| `services/langgraph/src/subgraphs/devops/env_analyzer.py` | 2 | Парсинг комментариев, контекст для LLM |
| `services/langgraph/tests/unit/test_secret_resolver.py` | 1, 2 | Обновить под новую логику |
| `services/langgraph/tests/unit/test_env_analyzer.py` | 2 | Тесты комментариев |
| `shared/clients/github.py` | 3 | `trigger_workflow_dispatch()` |
| `shared/models/service_deployment.py` | 3 | `deployed_sha` колонка |
| `shared/tests/clients/test_github.py` | 3 | 3 теста `trigger_workflow_dispatch` |
| `services/api/src/schemas/service_deployment.py` | 3 | `deployed_sha` в Create/Update/Read |
| `services/api/src/routers/service_deployments.py` | 3 | `deployed_sha` в create/update handlers |
| `services/langgraph/src/tools/__init__.py` | 3 | Убрать `delegate_ansible_deploy` |
| `services/infra-service/src/main.py` | 4 | Убрать deploy handler |
| `shared/queues.py` | 4 | Убрать `ansible:deploy:queue` |
| `shared/pyproject.toml` | 1 | Добавить `cryptography` |
| `docker-compose.yml` | 1 | `SECRETS_ENCRYPTION_KEY` env |
| `.env.example` | 1 | `SECRETS_ENCRYPTION_KEY` |
| `docs/NODES.md` | 3 | Обновить DevOps описание (Ansible → GitHub Actions) |
| `docs/CONTRACTS.md` | 3 | Убрать `ansible:deploy:queue` |
| `CLAUDE.md` | 3 | Обновить architecture (deploy → GitHub Actions) |

### Удаляемые файлы
| Файл | Итерация |
|------|----------|
| `services/langgraph/src/tools/devops_delegation.py` | 3 |
| `shared/schemas/deployment_jobs.py` | 3 |
| `services/langgraph/tests/service/test_deploy_flow.py` | 3 |
| `services/infra-service/src/deployer/` (каталог) | 4 |
