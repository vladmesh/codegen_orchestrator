# Агенты и Ноды

Каждый агент — это узел LangGraph с собственным набором инструментов и специализацией.

---

## 🧭 Product Owner (CLI Agent)

**Роль**: Центральный координатор на базе CLI-агента. Управляет всем жизненным циклом проекта через API tools.

**Реализация**: worker-manager создаёт Docker-контейнер с CLI агентом (Claude Code, Factory.ai или custom), который работает как Product Owner.

**Инструменты**: Все инструменты из API предоставляются через OpenAPI и native tool calling:
- `delegate_to_analyst`: делегирование анализа запроса
- `trigger_engineering`: запуск Engineering Subgraph
- `trigger_deploy`: запуск DevOps Subgraph
- `list_projects`, `get_project_status`: управление проектами
- `list_managed_servers`, `allocate_port`: управление инфраструктурой
- `save_project_secret`: сохранение секретов
- И другие...

**Выход**: Действия через tools, сообщения пользователю через Telegram

---

## 🧠 Analyst ⚠️ НЕ ИСПОЛЬЗУЕТСЯ

> **Статус**: Код есть (`nodes/analyst.py`), но НЕ подключен в graph. Планируется для Phase 3.

**Роль**: Первичный анализ запроса, уточнение требований, создание проекта.

**Когда вызывается**:
- Через `delegate_to_analyst` tool от PO
- При создании нового проекта

**Инструменты**:
- `list_projects`, `get_project_status`
- `create_project`: создаёт project record в БД
- Доступ к контексту предыдущих проектов

**Выход**: `current_project`, `project_spec` → переход к Zavhoz

---

## 🏠 Завхоз (Zavhoz) ⚠️ НЕ ИСПОЛЬЗУЕТСЯ

> **Статус**: Код есть (`nodes/zavhoz.py`), но НЕ подключен в graph. Ресурсы выделяются через `ResourceAllocator` (DevOps).

**Роль**: Управление ресурсами, изоляция секретов от LLM.

**Когда вызывается**:
- После Analyst (для нового проекта)
- Для выделения ресурсов перед деплоем

**Принцип**: LLM видит только handles, не реальные секреты.

**Инструменты**:
- `list_managed_servers`, `find_suitable_server`
- `allocate_port`, `get_next_available_port`
- `list_resource_inventory`

**Выход**: `allocated_resources` → переход к Engineering или DevOps

---

## 📐 Architect (Engineering Subgraph)

**Роль**: Проектирование структуры, создание GitHub репозитория, выбор модулей.

**Когда вызывается**:
- Первый этап Engineering Subgraph
- При необходимости изменить архитектуру

**Инструменты**:
- `create_github_repo`: создаёт репозиторий через GitHub App
- `select_modules`: выбор модулей из service-template
- `set_deployment_hints`: конфигурация для деплоя

**Выход**: `repo_info`, `selected_modules` → Scaffolder Service

---

## � Scaffolder Service (Async)

**Роль**: Асинхронный scaffolding проекта через Copier.

**Когда вызывается**:
- Автоматически после создания проекта через API (fire-and-forget)
- Отдельный Docker сервис, не часть LangGraph

**Сервис**: `services/scaffolder/`

**Действия**:
1. Слушает `scaffolder:queue` (Redis Stream)
2. Клонирует репозиторий
3. `copier copy` с выбранными модулями
4. Git commit + push
5. Обновляет `project.status = "scaffolded"` через API

**Выход**: `project.status = "scaffolded"` → DeveloperNode может начинать работу

---

## 👨‍💻 Developer (Engineering Subgraph)

**Роль**: Написание бизнес-логики в уже scaffolded проекте.

**Когда вызывается**:
- Первый этап Engineering Subgraph
- При rework от Tester (до 3 итераций)

**Реализация**:
1. Ждёт `project.status == "scaffolded"` (макс 5 мин, poll каждые 10s)
2. Спавнит контейнер через `worker-manager` (Claude Code / Factory.ai)
3. Worker-manager инжектит инструкции из `shared/prompts/developer_worker/INSTRUCTIONS.md` и `TASK.md` с project-specific задачей
4. Агент клонирует scaffolded repo и пишет бизнес-логику

**Валидация**: Проверяет наличие commit SHA в результате.

**Выход**: Код в репозитории → Tester

---

## 🧪 Tester (Engineering Subgraph)

**Роль**: Запуск тестов, проверка качества кода.

**Когда вызывается**:
- После Developer
- Финальный этап Engineering Subgraph

**Действия**:
- Запуск `make test`, `make lint`
- Проверка health endpoints (если задеплоено)

**Выход**:
- `test_results` с passed/failed/skipped
- При неудаче → возврат к Developer (max 3 итерации)
- При успехе → `engineering_status="done"` → DevOps

---

## 🔧 DevOps (Subgraph)

**Роль**: Деплой с интеллектуальным анализом секретов.

**Когда вызывается**:
- После Engineering Subgraph
- При `trigger_deploy` от PO

**Структура пакета** (`src/subgraphs/devops/`):
```
devops/
├── __init__.py          # Экспорты
├── state.py             # DevOpsState TypedDict
├── env_analyzer.py      # EnvAnalyzer + helper функции
├── nodes.py             # SecretResolver, ReadinessCheck, Deployer
└── graph.py             # Routing + create_devops_subgraph
```

**Ноды внутри subgraph**:

1. **EnvAnalyzer (LLM)**: Анализирует .env.example, классифицирует переменные
   - `infra`: генерируются автоматически (REDIS_URL, DATABASE_URL)
   - `computed`: вычисляются из контекста (APP_NAME, APP_ENV)
   - `user`: запрашиваются у пользователя (TELEGRAM_BOT_TOKEN)

2. **SecretResolver (Functional)**:
   - Генерирует infra секреты
   - Подставляет computed значения
   - Проверяет наличие user секретов

3. **ReadinessCheck (Functional)**:
   - Проверяет готовность к деплою
   - Если есть missing_user_secrets → возврат к PO
   - Если всё готово → Deployer

4. **Deployer (Functional)**:
   - Делегирует выполнение Ansible playbook в `infra-service` через Redis
   - Polling результата из `deploy:result:{request_id}`
   - Post-deployment операции:
     * Создает service deployment record в БД
     * Настраивает GitHub Actions CI secrets
     * Устанавливает статус проекта = active

**Архитектура**:
```
Deployer → delegate_ansible_deploy → Redis: deploy:queue
                                           ↓
                                    infra-service
                                           ↓
                                    Ansible Execution
                                           ↓
                                    Result in Redis
```

**Выход**:
- `deployed_url` при успехе
- `missing_user_secrets` если нужны секреты от пользователя

---

## 🚧 Infra Service

**Роль**: Изолированный сервис для выполнения Ansible операций (provisioning).

**Реализация**: Отдельный сервис `infra-service` для изоляции тяжёлых зависимостей (Ansible, SSH).

**Типы jobs**:
1. **Provisioning** (`provisioner:queue`):
   - Password reset через Time4VPS API
   - OS reinstall при необходимости
   - Ansible playbooks для настройки сервера
   - Редеплой сервисов после восстановления

**Архитектура**:
```
infra-service
  ├── Listen: provisioner:queue
  ├── Handlers:
  │   └── process_provisioner_job() → ansible_runner.py
  └── Publish: provisioner:results
```

**Выход**: Результаты в Redis Stream `provisioner:results`

---

## 🔄 Взаимодействие (CLI Agent Flow)

```
Пользователь (Telegram)
     │
     ▼
Telegram Bot → worker-manager
     │
     ▼
CLI Agent (Product Owner)
     │ tool calls via OpenAPI
     ├──────────────▶ respond (via Redis) ──▶ Пользователь
     ├──────────────▶ delegate_to_analyst ──▶ [Analyst ──▶ Zavhoz] (⚠️ не используется)
     │
     ├──────────────▶ trigger_engineering
     │                     │
     │      ┌──────────────┴──────────────┐
     │      ▼                              ▼
     │  Scaffolder Service (async)   Engineering Subgraph
     │  scaffolder:queue → copier    Developer (waits) → Tester
     │  → status=scaffolded ─────────────▶│
     │                                     │
     ├──────────────▶ trigger_deploy ◄─────┘
     │                     │
     │                     ▼
     │               DevOps Subgraph
     │               EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer
     │                                                      │
     └──────────────▶ (завершение) ◄─────────────────────────┘
```

**Важно**: CLI Agent координирует весь flow через API tools. Scaffolder работает асинхронно (fire-and-forget), DeveloperNode ждёт готовности scaffolding.
