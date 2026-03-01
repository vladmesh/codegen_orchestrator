# Codebase Refactoring Audit v2

> **Дата**: 2026-03-01
> **Ревизия**: v2 — исправлены неточности v1, добавлены пропущенные находки.

## Executive Summary

Архитектурные границы между микросервисами соблюдаются хорошо: сервисы не импортируют друг друга напрямую, общение через `shared/` и сетевые вызовы. Однако внутри `shared/` накопились расхождения между слоями (models vs DTOs), есть мёртвый конфиг от прошлых рефакторингов, и несколько "хабов" — файлов, которые взяли на себя слишком много.

---

## 1. Legacy & Dead Code

### 1.1 Deprecated CLI Command
`packages/orchestrator-cli/src/orchestrator_cli/commands/engineering.py:162` — команда `update_framework` помечена `deprecated=True` с предупреждением `[DEPRECATED] Update project framework using copier update.`

### 1.2 Deprecated CLI Argument
`scripts/seed_agent_configs.py:162` — флаг `--api-url` deprecated в пользу `--api-base-url`.

### 1.3 Legacy `(str, Enum)` синтаксис
21 instance по всему проекту. Python 3.12 поддерживает `enum.StrEnum`. Распределение:
- `shared/contracts/dto/` — 7 (ProjectStatus, ServiceModule, ServerStatus, TaskStatus, TaskType, IncidentSeverity, IncidentStatus)
- `shared/models/` — 7 (RAGScope, IncidentStatus, IncidentType, ServerStatus, DeploymentStatus, TaskType, TaskStatus)
- `shared/contracts/queues/` — 3 (DeployTrigger, AgentType, WorkerCapability + ещё WorkerChannels)
- `shared/schemas/` — 2 (ServiceModule, ToolGroup)
- `services/langgraph/src/schemas/` — 1 (EntryPoint)

### 1.4 Мёртвые per-file-ignores в `ruff.toml`
Три записи в `ruff.toml:31-33` ссылаются на **несуществующие файлы**:
```toml
"services/langgraph/src/nodes/product_owner.py" = ["C901", "PLR0912", "PLR0915"]
"services/langgraph/src/capabilities/base.py" = ["C901"]
"services/langgraph/src/worker.py" = ["PLR0915"]
```
Все три файла были удалены/рефакторены. Это мёртвый конфиг, который нужно вычистить.

### 1.5 Legacy Fallbacks
- `services/worker-manager/src/manager.py:525-530` — legacy networking fallback (`Empty DOCKER_NETWORK = use host networking (legacy)`). Docstring (строка 28) упоминает замену "legacy ContainerService and LifecycleManager".
- `services/scheduler/src/tasks/github_sync.py:213-226` — fallback поиска проектов по имени для "legacy projects or first sync".

### 1.6 Redundant Imports
`services/worker-manager/src/manager.py` — `import base64` на верхнем уровне (строка 1) и повторно внутри 4 методов (строки 586, 608, 629, 677). Внутренние импорты избыточны.

---

## 2. DRY Violations

### 2.1 `ServiceModule` — три источника правды
Одни и те же модули (`BACKEND`, `TG_BOT`, `NOTIFICATIONS`, `FRONTEND`) определены в трёх местах:
1. `shared/contracts/dto/project.py` — как `ServiceModule(str, Enum)`
2. `shared/schemas/modules.py` — как `ServiceModule(str, Enum)` (идентичный дубль)
3. `packages/orchestrator-cli/src/orchestrator_cli/commands/project.py:22` — как hardcoded список строк: `AVAILABLE_MODULES = ["backend", "tg_bot", "notifications", "frontend"]` (с комментарием `must match copier.yml`)

### 2.2 `MockProcess` в тестах worker-wrapper
Идентичный класс `MockProcess` дублируется в:
- `packages/worker-wrapper/tests/component/test_full_cycle.py:8-15`
- `packages/worker-wrapper/tests/unit/test_git_sha_extraction.py:35-42`

### 2.3 Test setup boilerplate
Разрозненная дупликация setup-логики (async task cancellation) между `test_notifications.py` и `test_proactive_listener.py`.

---

## 3. Enum Divergence между Models и DTOs (Data Integrity Risk)

Это серьёзнее, чем простое дублирование — enum-ы с одинаковыми именами имеют **разные значения** между уровнем БД (models) и API/контрактов (DTOs).

### 3.1 `ServerStatus`
| Model (`shared/models/server.py`) — 11 значений | DTO (`shared/contracts/dto/server.py`) — 8 значений |
|---|---|
| `discovered`, `pending_setup`, `provisioning`, `force_rebuild`, **`ready`**, **`in_use`**, **`error`**, `maintenance`, **`reserved`**, **`missing`**, **`decommissioned`** | **`new`**, `pending_setup`, `provisioning`, **`active`**, **`unreachable`**, `maintenance`, `force_rebuild`, `discovered` |

Только 5 значений совпадают. Модель не знает `new`/`active`/`unreachable`, DTO не знает `ready`/`in_use`/`error`/`reserved`/`missing`/`decommissioned`.

### 3.2 `IncidentStatus`
| Model (`shared/models/incident.py`) — 4 значения | DTO (`shared/contracts/dto/incident.py`) — 2 значения |
|---|---|
| `detected`, `recovering`, `resolved`, `failed` | `open`, `resolved` |

Единственное пересечение — `resolved`. Модель имеет `IncidentType` (4 значения), DTO имеет `IncidentSeverity` (4 значения) — это **разные концепции**, не аналоги.

**Risk**: Сервис, получающий DTO с `ServerStatus.ACTIVE`, не сможет корректно сохранить его в модель, где такого статуса нет. Или обратно — модель в статусе `IN_USE` не имеет DTO-представления.

---

## 4. Giant Files (>400 LOC)

### Tier 1: >600 LOC — приоритетные кандидаты на split
| Файл | Строки |
|---|---|
| `services/langgraph/src/workers/engineering_worker.py` | 947 |
| `shared/clients/github.py` | 863 |
| `services/worker-manager/src/manager.py` | 789 |
| `services/api/src/routers/rag.py` | 688 |
| `services/infra-service/src/provisioner/node.py` | 615 |

### Tier 2: 400–600 LOC — на контроле
| Файл | Строки |
|---|---|
| `services/langgraph/src/subgraphs/devops/nodes.py` | 516 |
| `services/telegram_bot/src/main.py` | 473 |
| `services/langgraph/src/subgraphs/devops/env_analyzer.py` | 462 |
| `services/scheduler/src/tasks/server_sync.py` | 411 |
| `services/langgraph/src/nodes/developer.py` | 405 |

### Tier 1 (тесты): >500 LOC
| Файл | Строки |
|---|---|
| `services/langgraph/tests/unit/workers/test_engineering_worker.py` | 656 |
| `services/worker-manager/tests/unit/test_project_id_passthrough.py` | 553 |

---

## 5. Code Smells

### 5.1 `sys.path` manipulation в telegram_bot
`services/telegram_bot/src/main.py:37` — `sys.path.insert(0, "/app")` с последующими 6 строками `# noqa: E402`. Хрупкий паттерн, зависящий от Docker layout.

### 5.2 `noqa`/`type: ignore` за пределами тестов
- `shared/__init__.py:7` — `RedisStreamClient = None  # type: ignore`
- `shared/redis/client.py:13,200` — `type: ignore` для optional redis import
- `scripts/test_e2e_analyst.py:16` — inline `noqa: C901`

---

## 6. TODO / HACK Technical Debt

### 6.1 Security Debt
- `services/api/src/routers/api_keys.py:36` — `TODO: Add real encryption here`
- `services/api/src/routers/api_keys.py:72` — `TODO: Add real decryption here`
- `services/api/src/routers/servers.py:66` — `TODO: Encrypt ssh_key`

### 6.2 Workflow Integration
- `services/api/src/routers/servers.py:282` — `TODO: Trigger LangGraph provisioner node via queue/webhook`

### 6.3 Implementation Debt
- `services/worker-manager/src/events.py:22` — `TODO: Implement actual event listening via DockerClient`
- `packages/worker-wrapper/src/worker_wrapper/main.py:63` — `Hack for now: signal handler cancels the task if run as task`
- `shared/notifications.py:143` — `TODO: Add is_admin field filtering when implemented`

---

## 7. Positive Findings

- **Чистые архитектурные границы**: сервисы не импортируют друг друга, зависимости идут через `shared/` и сеть.
- **Нет циклических импортов**: `TYPE_CHECKING` guards используются корректно (7 файлов).
- **Консистентное логирование**: structlog + `setup_logging()` применяется повсеместно.
- **Redis contracts**: после унификации (#3+#5 в backlog) все consumer'ы используют `RedisStreamClient.consume()` с pydantic контрактами.

---

## 8. Recommendations (по приоритету)

### Quick wins (< 30 мин)
1. Удалить 3 мёртвые записи из `ruff.toml` per-file-ignores.
2. Удалить deprecated `update_framework` CLI команду.
3. Убрать 4 redundant `import base64` в `manager.py`.

### Механическая работа (< 2 часа)
4. StrEnum миграция: глобальная замена `(str, Enum)` → `(StrEnum)` + `from enum import StrEnum` (21 файл).
5. Консолидировать `ServiceModule` — единый источник в `shared/contracts/dto/project.py`, удалить дубль из `shared/schemas/modules.py`, заменить hardcoded list в CLI на импорт enum.

### Требуют дизайн-решения
6. **Enum divergence** (ServerStatus, IncidentStatus) — решить: DTO должен зеркалить модель, или у них осознанно разные наборы? Это влияет на data integrity.
7. **Split giant files** — `engineering_worker.py` (947) и `github.py` (863) первые кандидаты. GitHub client → submodules (`repos`, `actions`, `secrets`). Engineering worker → вынести scaffold/CI/deploy phases.
8. **Security TODOs** — encryption/decryption в `api_keys.py` и `servers.py`. Приоритет зависит от того, насколько endpoints используются в production.
