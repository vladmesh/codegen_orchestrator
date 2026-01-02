# Проектирование секретницы (Secrets Vault)

> Дата: 2025-12-30
> Статус: Проектирование

> **⚠️ UPDATE 2026-01-02**: Часть архитектуры изменилась после рефакторинга Deploy Worker.
> - `run_ansible_deploy` удален, deployment теперь делегируется в `infrastructure-worker`
> - Ansible выполнение изолировано в отдельный сервис через Redis queue
> - См. `docs/refactoring/deploy-worker-refactor-2026-01-02.md`

## Содержание

1. [Проблема](#проблема)
2. [Текущее состояние](#текущее-состояние)
3. [Целевая архитектура](#целевая-архитектура)
4. [План итераций](#план-итераций)
5. [Технические детали](#технические-детали)
6. [Открытые вопросы](#открытые-вопросы)
7. [Синхронизация с service-template](#синхронизация-с-service-template)

---

## Проблема

### Текущие недостатки

1. **Секреты передаются в CLI аргументах Ansible**
   ```bash
   ansible-playbook --extra-vars '{"TELEGRAM_TOKEN": "123:ABC..."}'
   ```
   - Видны в `ps aux` на машине оркестратора
   - Попадают в логи subprocess
   - Остаются в памяти Python-процесса

2. **Секреты хранятся в открытом виде в БД**
   ```python
   project.config.secrets = {"TELEGRAM_BOT_TOKEN": "123:ABC..."}
   ```
   - Любой с доступом к БД видит все секреты
   - Нет шифрования at rest

3. **LLM видит секреты**
   - При вызове `save_project_secret(key, value)` — LLM передаёт value
   - Секреты остаются в контексте LLM (логи, память)

4. **Два источника правды**
   - Первый деплой: Ansible создаёт `.env` из extra-vars
   - Последующие деплои: GitHub Actions читает GitHub Secrets
   - Рассинхрон между ними ведёт к проблемам

5. **Нет метаинформации о секретах**
   - Заполнен ли секрет?
   - Какой длины?
   - Когда обновлялся?
   - Кто устанавливал?

---

## Текущее состояние

### Flow первого деплоя

```
┌─────────────────────────────────────────────────────────────────┐
│  DevOps Agent                                                    │
│       │                                                          │
│       ▼                                                          │
│  run_ansible_deploy(secrets={TELEGRAM_TOKEN: "123:ABC..."})     │
│       │                                                          │
│       ▼                                                          │
│  subprocess.run(["ansible-playbook", "--extra-vars",            │
│                  '{"TELEGRAM_TOKEN": "123:ABC..."}'])           │
│       │                                                          │
│       ▼                                                          │
│  Ansible создаёт .env на сервере                                │
│       │                                                          │
│       ▼                                                          │
│  _setup_ci_secrets() → GitHub Secrets (для будущих деплоев)     │
└─────────────────────────────────────────────────────────────────┘
```

### Flow последующих деплоев (CI/CD)

```
┌─────────────────────────────────────────────────────────────────┐
│  git push → GitHub Actions → читает GitHub Secrets → деплоит   │
│                                                                  │
│  НО: workflow не генерирует .env из секретов!                   │
│  Только docker compose pull && up                               │
└─────────────────────────────────────────────────────────────────┘
```

### Где хранятся секреты сейчас

| Тип | Хранилище | Проблема |
|-----|-----------|----------|
| User secrets | `project.config.secrets` (JSON в БД) | Открытый текст |
| Deploy secrets | GitHub Secrets | Write-only API |
| SSH ключ | `~/.ssh/id_ed25519` (volume) | Ок |
| GitHub App ключ | `~/.gemini/keys/` (volume) | Ок |

---

## Целевая архитектура

### Принцип: GitHub Secrets как единственный источник правды

```
┌─────────────────────────────────────────────────────────────────┐
│  PostgreSQL: project_secrets (ТОЛЬКО МЕТАДАННЫЕ)                │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ project_id | key              | type   | filled | synced  │ │
│  │ proj-123   | TELEGRAM_TOKEN   | user   | true   | true    │ │
│  │ proj-123   | POSTGRES_PASS    | infra  | true   | true    │ │
│  │ proj-123   | OPENAI_API_KEY   | user   | false  | false   │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  LLM видит: список ключей, типы, статус заполнения              │
│  LLM НЕ видит: значения секретов                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ sync при записи
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Secrets (ЗНАЧЕНИЯ)                                       │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ TELEGRAM_TOKEN = "123:ABC..." (encrypted by GitHub)        │ │
│  │ POSTGRES_PASS = "xK9m2..."    (encrypted by GitHub)        │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Деплой читает напрямую через ${{ secrets.* }}                  │
└─────────────────────────────────────────────────────────────────┘
```

### Новый flow деплоя

```
┌────────────────────────────────────────────────────────────────┐
│  1. Оркестратор собирает секреты                               │
│     - INFRA: генерит (POSTGRES_PASSWORD, APP_SECRET_KEY)       │
│     - USER: получает от пользователя (TELEGRAM_BOT_TOKEN)      │
│     - Сохраняет метаданные в БД                                │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  2. Заливаем ВСЕ секреты в GitHub Secrets                      │
│     github_client.set_repository_secrets(owner, repo, {        │
│       "POSTGRES_PASSWORD": "generated123",                     │
│       "TELEGRAM_BOT_TOKEN": "123:ABC...",                      │
│       "DEPLOY_HOST": "1.2.3.4",                                │
│     })                                                          │
│                                                                 │
│  Секреты передаются напрямую в GitHub API                      │
│  НЕ через CLI, НЕ через LLM контекст                           │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  3. Триггерим GitHub Actions workflow                          │
│     gh workflow run main.yml                                   │
└────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  4. GitHub Actions (main.yml в service-template):              │
│     - SSH на сервер                                             │
│     - Генерит .env из ${{ secrets.* }}                         │
│     - docker compose up                                         │
│                                                                 │
│  Секреты НИКОГДА не покидают GitHub!                           │
└────────────────────────────────────────────────────────────────┘
```

---

## План итераций

### Итерация 1: Модель метаданных секретов

**Цель:** Создать таблицу для хранения метаданных без значений.

**Задачи:**

1. **Создать модель `ProjectSecret`**
   - Файл: `shared/models/project_secret.py`
   ```python
   class ProjectSecret(Base):
       __tablename__ = "project_secrets"

       id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
       project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
       key: Mapped[str] = mapped_column(String(255))
       secret_type: Mapped[str] = mapped_column(String(50))  # user | infra | computed
       is_filled: Mapped[bool] = mapped_column(default=False)
       value_length: Mapped[int] = mapped_column(default=0)
       synced_to_github: Mapped[bool] = mapped_column(default=False)
       created_at: Mapped[datetime] = mapped_column(default=func.now())
       updated_at: Mapped[datetime] = mapped_column(onupdate=func.now())
       created_by: Mapped[str] = mapped_column(String(100))  # user | system | agent:devops

       __table_args__ = (UniqueConstraint("project_id", "key"),)
   ```

2. **Создать миграцию**
   ```bash
   make makemigrations MSG='add project_secrets table'
   ```

3. **Создать API endpoints**
   - Файл: `services/api/src/routers/secrets.py`
   ```python
   GET  /projects/{id}/secrets        # список метаданных
   POST /projects/{id}/secrets        # создать/обновить (value не хранится!)
   GET  /projects/{id}/secrets/missing # список незаполненных user-секретов
   ```

4. **Написать тесты**
   - `services/api/tests/unit/test_secrets_router.py`

---

### Итерация 2: Инструменты для LLM

**Цель:** Заменить текущие инструменты на безопасные версии.

**Задачи:**

1. **Новые tools для работы с секретами**
   - Файл: `services/langgraph/src/tools/secrets.py`
   ```python
   @tool
   async def list_project_secrets(project_id: str) -> list[dict]:
       """List all secrets for a project (metadata only, no values)."""
       # Returns: [{"key": "TOKEN", "type": "user", "filled": True, "length": 46}]

   @tool
   async def get_missing_secrets(project_id: str) -> list[str]:
       """Get list of unfilled user secrets."""
       # Returns: ["OPENAI_API_KEY"]

   @tool
   async def check_secret_filled(project_id: str, key: str) -> bool:
       """Check if a specific secret is filled."""
   ```

2. **Защищённый путь для сохранения секретов**
   - Файл: `services/langgraph/src/tools/secrets.py`
   ```python
   @tool
   async def save_user_secret(project_id: str, key: str, value: str) -> dict:
       """Save a user-provided secret.

       IMPORTANT: The value is immediately encrypted and sent to GitHub.
       It is NOT stored in our database, NOT logged, NOT returned.
       """
       # 1. Validate key exists in project_secrets with type=user
       # 2. Push to GitHub Secrets (encrypted by GitHub)
       # 3. Update metadata: is_filled=True, value_length=len(value), synced=True
       # 4. Return: {"saved": True, "key": key}  # NO VALUE IN RESPONSE
   ```

3. **Обновить DevOps tools**
   ```python
   @tool
   async def generate_infra_secrets(project_id: str) -> dict:
       """Generate all infrastructure secrets and push to GitHub."""
       # 1. Get list of infra secrets from project_secrets
       # 2. Generate values (POSTGRES_PASS, APP_SECRET_KEY, etc.)
       # 3. Push to GitHub Secrets
       # 4. Update metadata
       # 5. Return: {"generated": ["POSTGRES_PASS", "APP_SECRET_KEY"]}
   ```

4. **Удалить старый `save_project_secret`**
   - Или пометить как deprecated с warning

---

### Итерация 3: Интеграция с GitHub

**Цель:** Надёжная синхронизация с GitHub Secrets.

**Задачи:**

1. **Расширить GitHubAppClient**
   - Файл: `shared/clients/github.py`
   ```python
   async def list_repository_secrets(self, owner: str, repo: str) -> list[str]:
       """Get list of secret names (not values)."""

   async def delete_repository_secret(self, owner: str, repo: str, name: str) -> bool:
       """Delete a secret."""

   async def trigger_workflow(self, owner: str, repo: str, workflow: str, ref: str = "main") -> str:
       """Trigger a workflow and return run_id."""

   async def get_workflow_run_status(self, owner: str, repo: str, run_id: str) -> dict:
       """Get workflow run status."""

   async def wait_for_workflow(self, owner: str, repo: str, run_id: str, timeout: int = 300) -> dict:
       """Wait for workflow to complete."""
   ```

2. **Создать SecretsSyncService**
   - Файл: `shared/services/secrets_sync.py`
   ```python
   class SecretsSyncService:
       async def sync_project_secrets(self, project_id: str) -> dict:
           """Ensure all filled secrets are in GitHub."""
           # 1. Get all secrets from DB where is_filled=True
           # 2. Get list from GitHub
           # 3. Find missing in GitHub
           # 4. Re-push missing (need to re-read from... where?)
           # PROBLEM: We don't store values!
   ```

3. **Решить проблему re-sync**
   - Если секрет потерялся в GitHub — мы не можем восстановить
   - Варианты:
     a) Хранить encrypted copy в нашей БД (backup)
     b) Требовать повторный ввод от пользователя
     c) Считать GitHub единственным источником (no backup)
   - **Рекомендация:** Вариант (c) для MVP, добавить (a) позже

---

### Итерация 4: Замена Ansible на GitHub Actions

**Цель:** Убрать передачу секретов через CLI.

**Задачи:**

1. **Новый deploy tool**
   - Файл: `services/langgraph/src/tools/deploy.py`
   ```python
   @tool
   async def deploy_project(project_id: str) -> dict:
       """Deploy project via GitHub Actions workflow."""
       # 1. Check all secrets are synced to GitHub
       # 2. Trigger workflow: gh workflow run main.yml
       # 3. Wait for completion
       # 4. Return result
   ```

2. **Удалить/deprecate `run_ansible_deploy`**
   - Ansible остаётся для provisioning серверов
   - Но НЕ для деплоя приложений с секретами

3. **Обновить DevOps subgraph**
   - Заменить вызов `run_ansible_deploy` на `deploy_project`
   - Убрать передачу secrets в аргументах

4. **Обновить `_setup_ci_secrets`**
   - Переименовать в `setup_deploy_secrets`
   - Вызывать ДО деплоя, не после
   - Включить ВСЕ секреты (infra + user), не только deploy credentials

---

### Итерация 5: Валидация и аудит

**Цель:** Добавить проверки и логирование.

**Задачи:**

1. **Валидаторы форматов секретов**
   ```python
   SECRET_VALIDATORS = {
       "TELEGRAM_BOT_TOKEN": r"^\d+:[A-Za-z0-9_-]{35,}$",
       "OPENAI_API_KEY": r"^sk-[A-Za-z0-9]{32,}$",
       "POSTGRES_PASSWORD": r"^.{16,}$",  # min length
   }
   ```

2. **Аудит-лог**
   ```python
   class SecretAuditLog(Base):
       __tablename__ = "secret_audit_log"

       id: Mapped[UUID]
       project_id: Mapped[UUID]
       secret_key: Mapped[str]
       action: Mapped[str]  # created | updated | synced | deleted
       actor: Mapped[str]   # user:123 | agent:devops | system
       timestamp: Mapped[datetime]
       details: Mapped[dict]  # JSON with non-sensitive info
   ```

3. **Pre-deploy validation**
   - Проверить все required secrets заполнены
   - Проверить форматы (где есть валидаторы)
   - Проверить sync с GitHub

---

## Технические детали

### Категории секретов

| Тип | Примеры | Кто заполняет | Когда |
|-----|---------|---------------|-------|
| **infra** | `POSTGRES_PASSWORD`, `APP_SECRET_KEY` | DevOps автоматически | При первом деплое |
| **computed** | `APP_NAME`, `APP_ENV`, `REDIS_URL`, `POSTGRES_HOST` | DevOps из контекста | При деплое |
| **user** | `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY` | Пользователь через чат | До деплоя |

### Генерация и хранение infra-секретов

#### Кто генерирует?

Нода `secret_resolver` в **DevOps subgraph**:

```
┌─────────────────────────────────────────────────────────────────┐
│                     DEVOPS SUBGRAPH                              │
│                                                                  │
│  env_analyzer (LLM)                                              │
│      │ классифицирует: POSTGRES_PASSWORD → infra                │
│      │                 TELEGRAM_TOKEN → user                    │
│      ▼                                                          │
│  secret_resolver (functional)  ← ГЕНЕРИРУЕТ INFRA               │
│      │ 1. Генерирует: POSTGRES_PASSWORD = random()              │
│      │ 2. Проверяет user-секреты заполнены?                     │
│      │ 3. Пушит ВСЁ в GitHub Secrets                            │
│      ▼                                                          │
│  deployer (functional)                                           │
│      │ Триггерит: gh workflow run main.yml                      │
│      ▼                                                          │
│  END                                                             │
└─────────────────────────────────────────────────────────────────┘
```

#### Генераторы infra-секретов

```python
import secrets as secrets_module

INFRA_SECRET_GENERATORS = {
    "POSTGRES_PASSWORD": lambda: secrets_module.token_urlsafe(24),
    "APP_SECRET_KEY": lambda: secrets_module.token_urlsafe(32),
    "JWT_SECRET": lambda: secrets_module.token_urlsafe(32),
}
```

#### Жизненный цикл infra-секрета

```
┌─────────────────────────────────────────────────────────────────┐
│  ПЕРВЫЙ ДЕПЛОЙ                                                   │
│                                                                  │
│  1. secret_resolver генерирует POSTGRES_PASSWORD                │
│  2. Пушит в GitHub Secrets                                      │
│  3. Сохраняет metadata в БД (is_filled=true, length=32)         │
│  4. Значение НИГДЕ больше не хранится!                          │
│                                                                  │
│  GitHub Secrets = единственный источник правды                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  ПОСЛЕДУЮЩИЕ ДЕПЛОИ                                              │
│                                                                  │
│  1. secret_resolver проверяет: секрет уже есть в GitHub?        │
│  2. Если да → НЕ перегенерирует (иначе сломает БД)              │
│  3. Триггерит workflow                                           │
│  4. Workflow читает существующий секрет                          │
└─────────────────────────────────────────────────────────────────┘
```

#### Важно: проверка существования перед генерацией

```python
class SecretResolverNode(FunctionalNode):
    async def run(self, state: DevOpsState) -> dict:
        github = GitHubAppClient()
        owner, repo = parse_repo(state["repo_info"]["url"])

        # Проверяем какие секреты УЖЕ есть в GitHub
        existing = await github.list_repository_secrets(owner, repo)
        existing_names = {s["name"] for s in existing}

        secrets_to_push = {}

        for var, var_type in state["env_analysis"].items():
            if var_type == "infra":
                if var not in existing_names:
                    # Генерируем ТОЛЬКО если ещё нет
                    secrets_to_push[var] = INFRA_SECRET_GENERATORS[var]()
                # else: уже есть, не трогаем
            elif var_type == "user":
                if var in state["provided_secrets"]:
                    secrets_to_push[var] = state["provided_secrets"][var]

        # Пушим только новые/обновлённые
        if secrets_to_push:
            await github.set_repository_secrets(owner, repo, secrets_to_push)
```

#### Доступ после генерации

| Секрет | После записи в GitHub | Можно восстановить? |
|--------|----------------------|---------------------|
| POSTGRES_PASSWORD | Нет доступа (write-only API) | Можно перегенерить, но нужно сбросить в БД |
| APP_SECRET_KEY | Нет доступа | Можно перегенерить, но сессии инвалидируются |
| TELEGRAM_BOT_TOKEN | Нет доступа | Нужно спросить у пользователя заново |

#### Ротация infra-секретов

Если нужно сменить POSTGRES_PASSWORD:

1. Сгенерировать новый пароль
2. Обновить в GitHub Secrets
3. **Важно:** Обновить пароль в самой PostgreSQL (миграция или ручное вмешательство)
4. Редеплой

```python
@tool
async def rotate_infra_secret(project_id: str, key: str) -> dict:
    """Rotate an infrastructure secret. USE WITH CAUTION."""

    if key == "POSTGRES_PASSWORD":
        # Требует дополнительных действий на сервере
        return {
            "error": "POSTGRES_PASSWORD rotation requires manual DB update. "
                     "Use 'rotate_postgres_password' tool instead."
        }

    # Для других infra-секретов
    new_value = INFRA_SECRET_GENERATORS[key]()
    await github.set_repository_secret(owner, repo, key, new_value)

    return {"rotated": True, "key": key, "requires_redeploy": True}
```

### Как определяется тип секрета

DevOps LLM анализирует `.env.example` и классифицирует:

```python
ENV_ANALYZER_PROMPT = """
Classify each environment variable:

1. INFRA - Internal infrastructure (auto-generated):
   - Database URLs, Redis URLs
   - Internal service URLs
   - Secret keys, JWT secrets

2. COMPUTED - Derived from project context:
   - APP_NAME, APP_ENV
   - External-facing URLs

3. USER - External API keys (user must provide):
   - Third-party service tokens
   - OAuth credentials

When in doubt → USER (safer to ask).
"""
```

### Структура required_secrets в .env.example

Рекомендуемый формат для service-template:

```bash
# .env.example

# [infra] Database configuration
POSTGRES_DB=myapp
POSTGRES_USER=postgres
POSTGRES_PASSWORD=

# [infra] Redis
REDIS_URL=redis://redis:6379

# [computed] Application
APP_NAME=
APP_ENV=production

# [user] Telegram Bot (from @BotFather)
TELEGRAM_BOT_TOKEN=

# [user] OpenAI API
OPENAI_API_KEY=
```

Парсер определяет тип по комментарию `# [type]`.

### GitHub API ограничения

| Ограничение | Значение | Mitigation |
|-------------|----------|------------|
| Rate limit | 5000 req/hour | Batch operations |
| Secret name | 1-100 chars, A-Z_0-9 | Validate before push |
| Secret value | Max 64KB | Unlikely to hit |
| Secrets per repo | 100 | Enough for our use |

### Безопасность

1. **Секреты не логируются**
   - structlog с фильтрацией полей `*token*`, `*secret*`, `*password*`, `*key*`

2. **Секреты не в LLM контексте**
   - Tools возвращают только метаданные
   - Value передаётся напрямую в GitHub API

3. **Секреты не в git**
   - `.env` в `.gitignore`
   - Только `.env.example` с пустыми значениями

4. **Шифрование в GitHub**
   - NaCl SealedBox (libsodium)
   - Ключ репозитория от GitHub

---

## Открытые вопросы

### 1. Что делать если секрет потерялся в GitHub?

**Варианты:**
- (a) Хранить encrypted backup в нашей БД
- (b) Требовать повторный ввод
- (c) Считать потерю критической ошибкой

**Рекомендация:** (b) для user secrets, (a) для infra secrets (можно перегенерить).

### 2. Как обрабатывать ротацию секретов?

**Сценарий:** Пользователь хочет сменить TELEGRAM_BOT_TOKEN.

**Flow:**
1. Пользователь вводит новый токен через чат
2. `save_user_secret` обновляет в GitHub
3. Триггерим редеплой для применения

### 3. Как синхронизировать с существующими проектами?

**Миграция:**
1. Прочитать `project.config.secrets` (старое хранилище)
2. Для каждого секрета:
   - Создать запись в `project_secrets` (метаданные)
   - Запушить в GitHub Secrets (если есть repo)
   - Удалить из `config.secrets`

### 4. Нужен ли fallback на Ansible?

**Ситуация:** GitHub Actions недоступен или workflow сломан.

**Рекомендация:** Оставить `run_ansible_deploy` как emergency fallback, но:
- Не использовать по умолчанию
- Логировать warning при использовании
- Секреты всё равно брать из GitHub (через API, не через БД)

---

## Синхронизация с service-template

### Что нужно изменить в service-template

Задача добавлена в `/home/vlad/projects/service-template/docs/backlog.md`:
**"GitHub Secrets Integration for Deployment"**

Основные изменения:
1. `main.yml.jinja` — добавить генерацию `.env` из `${{ secrets.* }}`
2. `workflow_dispatch` trigger для ручного деплоя
3. Документация required secrets

### Порядок изменений

```
1. [service-template] Обновить main.yml.jinja
2. [service-template] Протестировать генерацию workflow
3. [orchestrator] Итерация 1-2 (модель + tools)
4. [orchestrator] Итерация 3 (GitHub integration)
5. [orchestrator] Итерация 4 (заменить Ansible)
6. [E2E] Протестировать полный flow на тестовом проекте
```

### Чеклист синхронизации

- [x] `main.yml.jinja` генерирует `.env` из secrets *(commit 74db801)*
- [ ] Orchestrator умеет `trigger_workflow`
- [ ] Orchestrator НЕ передаёт секреты в Ansible
- [ ] Все required secrets документированы
- [ ] Миграция существующих проектов работает

---

## Приоритеты

| Итерация | Приоритет | Effort | Impact | Зависимости |
|----------|-----------|--------|--------|-------------|
| 1. Модель метаданных | P0 | Low | Medium | - |
| 2. Tools для LLM | P0 | Medium | High | Итерация 1 |
| 3. GitHub интеграция | P0 | Medium | High | Итерация 2 |
| 4. Замена Ansible | P1 | Medium | High | Итерация 3 + service-template |
| 5. Валидация и аудит | P2 | Low | Medium | Итерация 4 |

**Рекомендуемый порядок:** 1 → 2 → 3 → 4 → 5

Итерации 1-3 можно делать параллельно с изменениями в service-template.
Итерация 4 требует завершения работ в обоих проектах.
