# Рефакторинг: Секреты и фильтрация проектов

> Дата: 2025-12-29
> Статус: В работе (Итерация 2 завершена)

## Содержание

1. [Выявленные проблемы](#выявленные-проблемы)
2. [Анализ причин](#анализ-причин)
3. [Архитектурное решение для секретов](#архитектурное-решение-для-секретов)
4. [План итераций](#план-итераций)
5. [Рекомендации](#рекомендации)

---

## Выявленные проблемы

### Проблема 1: ПО спрашивает технические секреты у пользователя

**Симптом:** При попытке деплоя бот спрашивает у пользователя `REDIS_URL`, `DATABASE_URL`, `APP_SECRET_KEY` и другие инфраструктурные параметры.

**Ожидание:** Пользователь не должен знать технические детали инфраструктуры. Он может предоставить только бизнес-секреты (токены внешних API).

### Проблема 2: ПО не знает GitHub репозиторий

**Симптом:** При запросе деплоя существующего проекта ПО спрашивает у пользователя URL репозитория.

**Ожидание:** Система должна знать репозиторий проекта, так как он создаётся Architect'ом.

### Проблема 3: "Мои проекты" показывает чужие проекты

**Симптом:** Админ при запросе "мои проекты" видит все проекты, включая ничейные (например, `palindrome_bot` без owner_id).

**Ожидание:** "Мои проекты" должны показывать только проекты текущего пользователя, даже для админа.

---

## Анализ причин

### Проблема 1: Технические секреты

**Где возникает:**

1. **Промпт Product Owner** (`agent_configs.system_prompt`):
   ```
   If activation requires secrets, ask for them and save with `save_project_secret`,
   then verify with `check_ready_to_deploy`.
   ```

2. **`inspect_repository`** (`services/langgraph/src/tools/activation.py:143-144`):
   ```python
   env_content = await github.get_file_contents(org, project_name, ".env.example")
   required_secrets = _parse_env_example(env_content) if env_content else []
   ```
   Парсит ВСЕ переменные из `.env.example` как required_secrets.

3. **`.env.example`** содержит смесь:
   ```bash
   # Инфраструктурные (должны генерироваться автоматически)
   REDIS_URL=redis://redis:6379
   DATABASE_URL=postgresql://...
   APP_SECRET_KEY=please-change-me

   # Пользовательские (нужно от пользователя)
   TELEGRAM_BOT_TOKEN=change-me
   ```

**Корень проблемы:** Система не различает типы секретов. Все missing secrets спрашиваются у пользователя.

### Проблема 2: GitHub репозиторий

**Где возникает:**

1. **`check_deploy_readiness`** (`services/langgraph/src/tools/deploy.py:58-60`):
   ```python
   if not project.get("repository_url"):
       missing.append("repository")
   ```

2. **Project model** не имеет `repository_url` заполненным после создания репозитория.

**Корень проблемы:** Architect создаёт репозиторий, но `repository_url` не сохраняется в project record в БД.

### Проблема 3: Фильтрация проектов

**Где возникает:**

`services/api/src/routers/projects.py:129-138`:
```python
if x_telegram_id is not None:
    user = await _resolve_user(x_telegram_id, db)
    if not user.is_admin:
        # Regular user: only their projects
        query = query.where(Project.owner_id == user.id)
    # ← Админ НЕ фильтруется! Видит ВСЕ проекты
```

**Корень проблемы:** Логика предполагает, что админ хочет видеть все проекты. Но "мои проекты" семантически означает "принадлежащие мне".

---

## Архитектурное решение для секретов

### Категории секретов

| Тип | Примеры | Кто заполняет | Когда |
|-----|---------|---------------|-------|
| **Инфраструктурные** | `REDIS_URL`, `DATABASE_URL`, `POSTGRES_*`, `APP_SECRET_KEY` | DevOps автоматически | При деплое |
| **Вычисляемые** | `APP_NAME`, `APP_ENV`, `BACKEND_API_URL` | DevOps из контекста проекта | При деплое |
| **Пользовательские** | `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `STRIPE_KEY` | Пользователь или пул ресурсов | До деплоя |

### Принцип: Кто добавляет переменную — тот определяет её тип

Отвергнутые подходы:
- **Whitelist инфра-секретов** — не масштабируется на новые переменные
- **Соглашение об именовании** — ненадёжно, легко ошибиться

### Выбранный подход: DevOps Subgraph с LLM-анализатором

DevOps становится subgraph с интеллектуальным анализом:

```
┌─────────────────────────────────────────────────────────────────┐
│                     DEVOPS SUBGRAPH                             │
│                                                                 │
│  env_analyzer (LLM)                                             │
│      ↓ читает .env.example, код, docker-compose                 │
│      ↓ классифицирует каждую переменную                         │
│                                                                 │
│  secret_resolver (functional)                                   │
│      ↓ генерирует infra секреты                                 │
│      ↓ подставляет computed                                     │
│      ↓ проверяет user секреты                                   │
│                                                                 │
│  [conditional]                                                  │
│      ↓ если missing_user_secrets → END (return to PO)           │
│      ↓ если всё ок → deployer                                   │
│                                                                 │
│  deployer (functional)                                          │
│      ↓ ansible playbook                                         │
│      ↓ END                                                      │
└─────────────────────────────────────────────────────────────────┘
```

**Flow при недостающих секретах:**

```
1. ПО вызывает trigger_deploy(project_id)
2. DevOps subgraph запускается
3. env_analyzer (LLM) анализирует код:
   - REDIS_URL → infra (генерируем)
   - DATABASE_URL → infra (генерируем)
   - TELEGRAM_BOT_TOKEN → user (нужно от пользователя)
4. secret_resolver генерирует infra, видит missing user secrets
5. Subgraph завершается с state.missing_user_secrets = ["TELEGRAM_BOT_TOKEN"]
6. ПО получает результат, видит missing secrets
7. ПО решает: спросить у Zavhoz (если про ресурсы) или у пользователя
8. Пользователь предоставляет токен
9. ПО сохраняет секрет, повторно вызывает trigger_deploy
10. DevOps видит все секреты, деплоит
```

---

## План итераций

### Итерация 0: Quick fixes (не требуют архитектурных изменений)

**Цель:** Исправить очевидные баги без переделки архитектуры.

**Задачи:**

1. **[DONE] Фильтрация проектов для админа**
   - Файл: `services/api/src/routers/projects.py`
   - Добавить query parameter `owner_only=true`
   - Или: всегда фильтровать по owner_id, добавить отдельный endpoint `/projects/all` для админов

2. **[DONE] Сохранение repository_url в project**
   - Файл: `services/langgraph/src/nodes/architect.py` или `tools/architect_tools.py`
   - После создания репозитория вызывать `api_client.patch(f"/projects/{project_id}", {"repository_url": repo_url})`

3. **[DONE] Убрать инструкцию про секреты из промпта ПО**
   - Таблица: `agent_configs` WHERE id = 'product_owner'
   - Удалить: "If activation requires secrets, ask for them..."

**Рекомендации к итерации 0:**
- Начать с этого, так как это быстрые улучшения
- Тестировать каждое изменение отдельно
- Не ломает существующий flow

---

### Итерация 1: DevOps как LLMNode

**Цель:** Превратить DevOps в интеллектуальную ноду, способную анализировать код.

**Задачи:**

1. **[DONE] Создать DevOps LLMNode**
   - Файл: `services/langgraph/src/nodes/devops.py`
   - Наследовать от `LLMNode` вместо `FunctionalNode`
   - Добавить config в `agent_configs` таблицу

2. **[DONE] Создать tools для DevOps**
   - Файл: `services/langgraph/src/tools/devops_tools.py`
   ```python
   @tool
   async def analyze_env_requirements(project_id: str) -> dict:
       """Analyze .env.example and classify each variable."""

   @tool
   async def generate_infra_secret(key: str, project_id: str) -> str:
       """Generate infrastructure secret value."""

   @tool
   async def get_project_context(project_id: str) -> dict:
       """Get project context for computed secrets."""

   @tool
   async def run_ansible_deploy(project_id: str, secrets: dict) -> dict:
       """Execute ansible deployment."""
   ```

3. **[DONE] Написать system prompt для DevOps**
   ```
   You are the DevOps engineer responsible for deploying projects.

   When deploying, you must:
   1. Analyze .env.example using analyze_env_requirements
   2. For each variable, determine its type:
      - INFRA: internal infrastructure (Redis, DB, internal URLs)
      - COMPUTED: derived from project context (APP_NAME, APP_ENV)
      - USER: external API keys that user must provide
   3. Generate INFRA secrets using generate_infra_secret
   4. Get COMPUTED values from get_project_context
   5. For USER secrets: if missing, return error with list of needed secrets
   6. If all secrets ready, run_ansible_deploy
   ```

**Рекомендации к итерации 1:**
- DevOps LLM должен быть консервативным — лучше спросить лишнее, чем сгенерировать неправильное
- Логировать все решения LLM для отладки

---

### Итерация 2: DevOps Subgraph с возвратом к ПО

**Цель:** Реализовать полноценный subgraph с механизмом возврата недостающих секретов к ПО.

**Задачи:**

1. **[DONE] Создать DevOps subgraph**
   - Файл: `services/langgraph/src/subgraphs/devops.py`
   ```python
   class DevOpsState(TypedDict):
       # Input
       project_id: str
       project_spec: dict
       allocated_resources: dict
       repo_info: dict
       provided_secrets: dict  # секреты от ПО

       # Internal
       env_analysis: dict  # {var: "infra"|"computed"|"user"}
       resolved_secrets: dict

       # Output
       missing_user_secrets: list[str]
       deployment_result: dict | None
       deployed_url: str | None
   ```

2. **[DONE] Реализовать ноды subgraph**
   - `env_analyzer` (LLM): классифицирует переменные
   - `secret_resolver` (functional): генерирует infra, подставляет computed
   - `readiness_check` (functional): проверяет user secrets
   - `deployer` (functional): ansible

3. **[DONE] Интегрировать в main graph**
   - Файл: `services/langgraph/src/graph.py`
   - Заменить `devops` node на `devops_subgraph`
   - Добавить routing: если `missing_user_secrets` → вернуть к ПО

4. **[TODO] Обновить ПО для обработки missing secrets**
   - Tool `trigger_deploy` должен возвращать `missing_secrets` если есть
   - ПО должен уметь собирать секреты и передавать повторно
   - *(Отложено: требует доработки PO tools и промпта)*

**Рекомендации к итерации 2:**
- Использовать checkpointer для сохранения состояния между вызовами
- DevOps subgraph должен быть идемпотентным
- Все секреты собирать за один раз (не по одному)

---

### Итерация 3: Пул ресурсов (Telegram боты)

**Цель:** Реализовать автоматическое выделение Telegram ботов из пула.

**Задачи:**

1. **API для управления пулом ботов**
   - Endpoint: `POST /api/telegram-bots` (регистрация бота админом)
   - Endpoint: `GET /api/telegram-bots/available` (список свободных)
   - Endpoint: `POST /api/telegram-bots/{id}/allocate` (привязка к проекту)

2. **Расширить Zavhoz**
   - Tool: `allocate_telegram_bot(project_id)` — выделяет бота из пула
   - Tool: `release_telegram_bot(project_id)` — освобождает при удалении проекта

3. **Интегрировать в DevOps flow**
   - Если проект требует `TELEGRAM_BOT_TOKEN` и нет в secrets
   - DevOps возвращает `missing: ["TELEGRAM_BOT_TOKEN"]`
   - ПО проверяет: есть ли в пуле? Если да → Zavhoz allocate. Если нет → спросить у пользователя.

**Рекомендации к итерации 3:**
- Начать с простого: один бот = один проект
- Токены хранить зашифрованными (`token_enc` уже есть в схеме)
- Добавить возможность пользователю использовать свой бот вместо пула

---

## Рекомендации

### Общие рекомендации

1. **Итеративность** — каждая итерация должна быть deployable и не ломать существующий функционал

2. **Тестирование** — писать интеграционные тесты для каждого изменения в flow

3. **Логирование** — все решения LLM по классификации секретов логировать для отладки

4. **Backwards compatibility** — старые проекты с секретами в `config.secrets` должны продолжать работать

### Рекомендации по коду

1. **DevOps LLM prompt должен быть в БД** — как у других агентов, для возможности редактирования без деплоя

2. **Секреты не должны попадать в логи** — использовать `structlog` с фильтрацией sensitive полей

3. **Генерация infra-секретов** — вынести в отдельный модуль `shared/secrets.py`:
   ```python
   def generate_database_url(project_name: str, server: dict) -> str:
       ...

   def generate_redis_url(server: dict) -> str:
       ...

   def generate_secret_key() -> str:
       return secrets.token_urlsafe(32)
   ```

### Рекомендации по архитектуре

1. **Separation of concerns:**
   - DevOps — только деплой
   - Zavhoz — только ресурсы (серверы, порты, боты)
   - ПО — только координация

2. **Single responsibility для секретов:**
   - Классификация: DevOps LLM
   - Генерация infra: DevOps functional tool
   - Выделение из пула: Zavhoz
   - Запрос у пользователя: ПО

3. **Fail-safe поведение:**
   - Если LLM не уверен в классификации → считать user secret
   - Лучше спросить лишний раз, чем сгенерировать неправильно

---

## Приоритеты

| Итерация | Приоритет | Effort | Impact |
|----------|-----------|--------|--------|
| 0. Quick fixes | P0 (завершена) | Low | High |
| 1. DevOps LLMNode | P1 | Medium | High |
| 2. DevOps Subgraph | P1 | High | High |
| 3. Пул ресурсов | P2 | Medium | Medium |

**Рекомендуемый порядок:** 0 → 1 → 2 → 3

Итерации 1-2 делаем сразу как целевое решение, без временных костылей.
