# Brainstorm: Domain Model — Project vs Service Status Split

> **Дата**: 2026-03-12
> **Контекст**: ProjectStatus смешивает три ортогональных измерения (lifecycle, work, deployment), что приводит к recurring bug "project stuck at developing". Нужно пересмотреть абстракции.
> **Status**: done

---

## Current State

### Один enum на всё

`ProjectStatus` содержит 13 значений из трёх разных измерений:

| Измерение | Статусы |
|-----------|---------|
| Lifecycle | `draft`, `scaffolding`, `scaffolded`, `archived` |
| Work activity | `developing`, `testing` |
| Deployment | `deploying`, `active`, `maintenance` |
| Errors | `error`, `failed`, `missing`, `scaffold_failed` |

Engineering consumer ставит `developing` → затирает `active`. Deploy consumer ставит `deploying` → затирает `developing`. При сбое между шагами статус "зависает" в промежуточном значении. Это recurring bug из двух последних e2e отчётов.

### Что уже есть кроме Project

- **ServiceDeployment** — модель в БД (`service_deployments`), отслеживает что задеплоено на каком сервере. Имеет свой `DeploymentStatus` (`running`, `stopped`, `failed`, `pending`). Создаётся в devops/nodes.py после успешного деплоя.
- **Run** — отслеживает выполнение (engineering run, deploy run). Имеет `RunStatus` (`queued`, `running`, `completed`, `failed`).
- **Story/Task** — единицы работы. Story: `created → in_progress → deploying → completed`. Task: `backlog → todo → in_dev → ... → done`.

### Где project.status используется для решений

| Место | Логика | Что на самом деле проверяется |
|-------|--------|-------------------------------|
| `webhooks.py:108` | `!= ACTIVE` → reject webhook | "Был ли хоть один успешный деплой?" |
| `task_dispatcher.py:284` | `== ACTIVE` → action=feature | "Есть ли service dir на сервере?" |
| `task_dispatcher.py:143` | `in (DRAFT, SCAFFOLDING, SCAFFOLD_FAILED)` → skip | "Готов ли проект к работе?" |
| `engineering.py:364` | `== DRAFT` → trigger scaffold | "Есть ли repo/scaffold?" |
| `po/tools.py:231` | `== DRAFT` → action=create | "Первая story или нет?" |

Ни одно из этих мест на самом деле не спрашивает "что за активность сейчас происходит". Все спрашивают одно из двух: **"готов ли проект к работе?"** или **"был ли успешный деплой?"**.

## Problem

Одно поле `project.status` обслуживает три разных вопроса:

1. **Lifecycle**: готов ли проект к работе? (draft → active → archived)
2. **Service state**: что с работающим сервисом? (not_deployed → running → down)
3. **Current activity**: что происходит прямо сейчас? (developing, deploying)

Вопрос #3 — это **не состояние**, а наличие активного Run/Story. Записывать его в статус родительской сущности — архитектурная ошибка. Именно она создаёт все race conditions и stuck states.

## Ключевой принцип

**Статус сущности = наблюдаемое состояние. Активность = наличие активного child-entity (Run, Story).**

- "Проект в разработке" = есть story в `in_progress`. Не нужен статус `developing`.
- "Сервис деплоится" = есть run типа `deploy` в `running`. Не нужен статус `deploying`.
- "Сервис работает" = последний ServiceDeployment.status == `running`. Не нужен `active` на Project.

## Options

### Option A: Два поля на Project (`status` + `service_status`)

Добавить `service_status` к Project. Убрать work/deployment значения из `ProjectStatus`.

```python
class ProjectStatus(StrEnum):
    DRAFT = "draft"           # Только создан, нет repo
    SCAFFOLDING = "scaffolding"  # scaffold run в процессе
    ACTIVE = "active"         # Готов к работе
    PAUSED = "paused"         # Заморожен пользователем
    ARCHIVED = "archived"     # Мёртв

class ServiceStatus(StrEnum):
    NOT_DEPLOYED = "not_deployed"
    RUNNING = "running"
    DEGRADED = "degraded"     # deploy ok, smoke failed
    DOWN = "down"             # health check failed
    STOPPED = "stopped"       # остановлен намеренно
```

**(+)** Минимальное изменение схемы — одна колонка
**(+)** Оба статуса на Project → один запрос, не нужен JOIN
**(+)** Все текущие decision points легко мигрируются
**(-)** `scaffolding` и `scaffold_failed` — это снова "активность как статус". Scaffold = Run, а не project state
**(-)** Если проект когда-нибудь получит staging + prod — service_status одного поля не хватит

### Option B: Derive всё из child entities

Project.status = только lifecycle (`draft → active → paused → archived`).
Service state = derive из `ServiceDeployment.status` (уже есть!).
Work state = derive из наличия `Story(status=in_progress)`.
Deploy state = derive из наличия `Run(type=deploy, status=running)`.

```python
class ProjectStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"

# Вместо project.status == ACTIVE для deploy_action:
deployment = await api.get_latest_deployment(project_id)
deploy_action = "feature" if deployment else "create"

# Вместо project.status != ACTIVE для webhook:
deployment = await api.get_latest_deployment(project_id)
if not deployment or deployment.status != "running":
    reject()
```

**(+)** Чистая модель — статус = факт, не процесс
**(+)** Невозможен stuck state — нечему "зависнуть"
**(+)** ServiceDeployment уже существует в БД и уже заполняется devops/nodes.py
**(+)** Scaffolding = scaffold Run, не project.status. `DRAFT → ACTIVE` когда Run completed
**(-)** Больше запросов (нужен JOIN или отдельный запрос за deployment)
**(-)** Нужно убедиться что ServiceDeployment создаётся/обновляется надёжно
**(-)** Миграция сложнее — нужно обновить все decision points

### Option C: Гибрид — два поля + derive activity

Project: `status` (lifecycle) + `service_status` (cached from ServiceDeployment).
Activity: derive из Run/Story (никогда не записывать в project).

```python
class ProjectStatus(StrEnum):
    DRAFT = "draft"       # Нет repo/scaffold
    ACTIVE = "active"     # Готов к работе (scaffold done)
    PAUSED = "paused"     # Заморожен
    ARCHIVED = "archived" # Мёртв

class ServiceStatus(StrEnum):
    NOT_DEPLOYED = "not_deployed"
    RUNNING = "running"
    DEGRADED = "degraded"
    DOWN = "down"
    STOPPED = "stopped"
```

`service_status` обновляется **только deploy worker** при завершении и **health checker** при проверках. Engineering consumer **никогда не трогает** project.

**(+)** Простота запросов (оба поля на Project)
**(+)** Чистое разделение: lifecycle vs runtime
**(+)** Activity не записывается в статус → нет stuck states
**(+)** `service_status` = кэш от ServiceDeployment, source of truth остаётся в ServiceDeployment
**(-)** Дублирование: service_status на Project + status на ServiceDeployment. Может разъехаться

## Scaffolding — отдельный вопрос

Сейчас `SCAFFOLDING → SCAFFOLDED → SCAFFOLD_FAILED` — три значения ProjectStatus ради одного процесса.

С новой моделью: scaffold = Run. Project сидит в `DRAFT` пока scaffold run не станет `completed`. Тогда Project → `ACTIVE`. Если run `failed` — Project остаётся `DRAFT`, ошибка на Run.

Это устраняет ещё три значения из enum и делает scaffold обычным async процессом как engineering/deploy.

## `MISSING` — не статус проекта

`MISSING` = "repo не нашёлся на GitHub при sync". Проект не может быть missing — missing может быть **репозиторий**.

**Решение**: убрать `MISSING` из ProjectStatus. Перенести на Repository — либо как `Repository.status`, либо как флаг. github_sync работает с репозиториями, а не с проектами, это его natural scope.

## Decisions

**Option C** — гибрид. Два поля на Project + activity derive из child entities.

### Уточнения (из обсуждения)

1. **`MISSING` — не статус проекта, а статус репозитория.** Переносится на Repository.
2. **`DeploymentStatus` и `ServiceStatus` — разные enum'ы.** DeploymentStatus = статус процесса (Run/попытка задеплоить): `pending`, `running`, `failed`. ServiceStatus = наблюдаемое состояние runtime: `not_deployed`, `running`, `degraded`, `down`, `stopped`. Разные плоскости — процесс vs состояние.
3. **Одна задача** — split status + scaffold-as-run + MISSING migration в одном task.

### Итоговая модель

```python
class ProjectStatus(StrEnum):
    """Lifecycle — готов ли проект к работе."""
    DRAFT = "draft"       # Нет repo/scaffold
    ACTIVE = "active"     # Готов к работе
    PAUSED = "paused"     # Заморожен пользователем
    ARCHIVED = "archived" # Мёртв

class ServiceStatus(StrEnum):
    """Runtime — наблюдаемое состояние сервиса."""
    NOT_DEPLOYED = "not_deployed"
    RUNNING = "running"
    DEGRADED = "degraded"   # deploy ok, smoke failed
    DOWN = "down"           # health check failed
    STOPPED = "stopped"     # остановлен намеренно

class DeploymentStatus(StrEnum):
    """Process — статус попытки деплоя (уже есть на ServiceDeployment/Run)."""
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
```

### Принципы

- **Статус = наблюдаемое состояние.** Не процесс, не активность.
- **Активность = наличие активного child entity.** "В разработке" = Story in_progress. "Деплоится" = Run(deploy) running.
- **Engineering consumer никогда не трогает project.** Ни status, ни service_status.
- **Deploy worker** обновляет только `service_status`. Не `status`.
- **Scaffold = Run.** Project сидит в `DRAFT` пока scaffold Run не completed → `ACTIVE`.

### Миграция decision points

| Было | Станет |
|------|--------|
| `project.status == ACTIVE` → webhook ok | `project.service_status == RUNNING` |
| `project.status == ACTIVE` → action=feature | `project.service_status != NOT_DEPLOYED` |
| `project.status in (DRAFT, SCAFFOLDING, ...)` → skip dispatch | `project.status == DRAFT` |
| `project.status == DRAFT` → trigger scaffold | `project.status == DRAFT` (без изменений) |
| engineering sets `DEVELOPING` | **удалить** (activity = Story in_progress) |
| deploy sets `DEPLOYING` | **удалить** (activity = Run running) |
| deploy success sets `ACTIVE` | deploy sets `service_status = RUNNING` |
| github_sync sets `MISSING` | sets `repository.status = MISSING` |

## Scope of change

- **DB migration**: добавить `service_status` колонку на Project, убрать старые значения из `status`, мигрировать данные
- **ProjectStatus enum**: сократить до 4 значений (draft, active, paused, archived)
- **ServiceStatus enum**: новый, на Project
- **DeploymentStatus**: оставить как есть (на ServiceDeployment) — это статус процесса
- **Repository**: добавить status field (для MISSING)
- **engineering.py**: убрать все `project.status = DEVELOPING/DEPLOYING`
- **deploy.py**: менять `service_status` вместо `status`
- **task_dispatcher.py**: проверять `service_status` для deploy_action
- **webhooks.py**: проверять `service_status` для webhook gate
- **scaffolder**: при success ставить `project.status = ACTIVE` вместо `SCAFFOLDED`
- **github_sync**: ставить `repository.status = MISSING` вместо `project.status = MISSING`
- **health_checker**: обновлять `service_status` по результатам проверок

~15 файлов, ~30 точек изменения.

## Action Items

- → new task: "Split ProjectStatus: lifecycle (status) + runtime (service_status) + scaffold-as-run + MISSING on Repository"
- → idea: "ServiceDeployment как source of truth для service_status с event-driven sync"
- → idea: "Staging/production environments — ServiceDeployment per environment" (YAGNI)
- → backlog #1006: пересмотреть scope — decouple deploy from story lifecycle частично решается этим split'ом
