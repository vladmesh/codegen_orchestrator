# Агенты и Ноды

Каждый агент — это узел LangGraph с собственным набором инструментов и специализацией.

---

## 🧭 Product Owner (CLI Agent)

**Роль**: Центральный координатор на базе CLI-агента. Управляет всем жизненным циклом проекта через API tools.

**Реализация**: workers-spawner создаёт Docker-контейнер с CLI агентом (Claude Code, Factory.ai или custom), который работает как Product Owner.

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

## 🧠 Analyst

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

## 🏠 Завхоз (Zavhoz)

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

**Выход**: `repo_info`, `selected_modules` → Preparer

---

## 🔧 Preparer (Engineering Subgraph)

**Роль**: Scaffolding проекта через Copier, коммит начальной структуры.

**Когда вызывается**:
- После Architect в Engineering Subgraph
- Functional node (не LLM)

**Действия**:
1. `copier copy` с выбранными модулями
2. Записывает TASK.md, AGENTS.md
3. Git commit + push

**Выход**: `repo_prepared=True`, `preparer_commit_sha` → Developer

---

## 👨‍💻 Developer (Engineering Subgraph)

**Роль**: Написание бизнес-логики через Factory.ai Droid.

**Когда вызывается**:
- После Preparer
- При rework от Tester (до 3 итераций)

**Реализация**: Спавнит контейнер через `workers-spawner` сервис (Factory Droid или Claude Code).

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
   - Делегирует выполнение Ansible playbook в `infrastructure-worker` через Redis
   - Polling результата из `deploy:result:{request_id}`
   - Post-deployment операции:
     * Создает service deployment record в БД
     * Настраивает GitHub Actions CI secrets
     * Устанавливает статус проекта = active

**Архитектура**:
```
Deployer → delegate_ansible_deploy → Redis: ansible:deploy:queue
                                           ↓
                                    infrastructure-worker
                                           ↓
                                    Ansible Execution
                                           ↓
                                    Result in Redis
```

**Выход**:
- `deployed_url` при успехе
- `missing_user_secrets` если нужны секреты от пользователя

---

## 🚧 Infrastructure Worker

**Роль**: Изолированный сервис для выполнения Ansible операций (provisioning и deployment).

**Реализация**: Отдельный сервис `infrastructure-worker` для изоляции тяжёлых зависимостей (Ansible, SSH).

**Типы jobs**:
1. **Provisioning** (`provisioner:queue`):
   - Password reset через Time4VPS API
   - OS reinstall при необходимости
   - Ansible playbooks для настройки сервера
   - Редеплой сервисов после восстановления

2. **Deployment** (`ansible:deploy:queue`):
   - Выполнение Ansible playbook для деплоя проектов
   - Делегируется из DeployerNode (langgraph)
   - Результаты возвращаются через Redis: `deploy:result:{request_id}`

**Архитектура**:
```
infrastructure-worker
  ├── Listen: provisioner:queue + ansible:deploy:queue
  ├── Handlers:
  │   ├── process_provisioner_job() → ansible_runner.py
  │   └── process_deployment_job() → deployment_executor.py
  └── Publish: {provisioner|deploy}:result:{request_id}
```

**Выход**: Результаты в Redis с TTL 1 час

---

## 🔄 Взаимодействие (CLI Agent Flow)

```
Пользователь (Telegram)
     │
     ▼
Telegram Bot → workers-spawner
     │
     ▼
CLI Agent (Product Owner)
     │ tool calls via OpenAPI
     ├──────────────▶ respond (via Redis) ──▶ Пользователь
     ├──────────────▶ delegate_to_analyst ──▶ Analyst ──▶ Zavhoz
     │                                                      │
     ├──────────────▶ trigger_engineering ◄─────────────────┘
     │                     │
     │                     ▼
     │               Engineering Subgraph
     │               Architect → Preparer → Developer → Tester
     │                                                      │
     ├──────────────▶ trigger_deploy ◄──────────────────────┘
     │                     │
     │                     ▼
     │               DevOps Subgraph
     │               EnvAnalyzer → SecretResolver → ReadinessCheck → Deployer
     │                                                      │
     └──────────────▶ (завершение) ◄─────────────────────────┘
```

**Важно**: CLI Agent координирует весь flow через API tools. Subgraphs (Engineering, DevOps) работают асинхронно через Redis queues.
