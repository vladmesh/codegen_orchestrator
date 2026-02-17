# E2E Investigation: reverse-echo-bot deploy failure

> **Date**: 2026-02-17
> **Project**: reverse-echo-bot (project_id: `9069ec17`)
> **Branch**: feat/deploy-architecture
> **Status**: 2 бага найдено, фикс требуется в двух проектах

---

## Timeline

```
03:47:56 — Services started (deploy-worker, engineering-worker, langgraph, etc.)
03:48:08 — PO: создал проект reverse-echo-bot, аллоцировал серверы
03:48:12 — Provisioner: fire-and-forget для vps-267179 (уже готов), vps-267180 (провижен за 49с)
03:50:24 — Engineering: порты аллоцированы (backend:8000, tg_bot:8001)
03:50:24 — Engineering: ждёт scaffolding
03:50:26 — PO: set_reminder(12 min, "check engineering task eng-08a7fa1bf1dd")
03:50:39 — CI #22085272459: scaffold commit CI passed
03:50:44 — Scaffolding complete, worker spawn requested
03:51:09 — Worker created: worker-dev-reverse-echo-bot-e552f7d7
03:51:09 — Claude Code started inside worker
03:58:56 — Claude Code: commit 75f1a36 "feat: implement reverse-echo bot message handler"
03:59:07 — CI #22085418134: developer commit CI started
03:59:19 — Worker wrapper: captured commit_sha=75f1a36, session_id=b673eb90
03:59:19 — Engineering: developer_node_success, начал ждать CI
04:02:13 — CI #22085418134: passed → engineering: ci_check_passed
04:02:13 — Engineering: deploy_auto_triggered → deploy-08a7fa1bf1dd         ← DEPLOY #1
04:02:13 — Deploy-worker: подхватил deploy-08a7fa1bf1dd
04:02:16 — Deploy: env_analyzer(16 vars), secret_resolver(5 infra secrets), readiness_check OK
04:02:25 — Deploy: secrets записаны, workflow_dispatch отправлен
04:02:27 — Deploy.yml #22085479157: started
04:02:38 — PO reminder fired: "check engineering task eng-08a7fa1bf1dd status"
04:02:42 — PO: проверил task → завершён, вызвал trigger_deploy              ← DEPLOY #2
04:02:42 — PO: po_deploy_triggered → deploy-c352c9875cab
04:03:16 — Deploy.yml #22085479157: FAILED (.env.prod not found)
04:03:16 — Deploy-worker: deploy-08a7fa1bf1dd FAILED
04:03:17 — Deploy-worker: подхватил deploy-c352c9875cab (уже в очереди)
04:03:19 — Deploy: повторная env_analyzer, secret_resolver, readiness_check
04:03:20 — Deploy: записал все 9 secrets заново
04:03:29 — Deploy: workflow_dispatch #2
04:03:30 — Deploy.yml #22085499900: started
04:03:56 — Deploy.yml #22085499900: FAILED (.env.prod not found) — та же ошибка
04:04:03 — Deploy-worker: deploy-c352c9875cab FAILED
```

---

## BUG 7: Дублирующий deploy — PO reminder race condition

### Суть

Два deploy-а запущены параллельно для одного проекта. Первый — от engineering-worker (auto-trigger после CI), второй — от PO (по reminder'у через 12 минут).

### Три источника deploy задач

В системе существует **три независимых пути** создания deploy задач:

| # | Источник | Файл | task_id формат | Когда срабатывает |
|---|----------|------|----------------|-------------------|
| A | Engineering worker | `workers/engineering_worker.py:706` | `deploy-{eng_suffix}` | CI passed → auto-trigger |
| B | GitHub webhook | `api/routers/webhooks.py:121` | `deploy-wh-{uuid[:8]}` | CI success webhook, project.status == "active" |
| C | PO tool | `po/tools.py:223` | `deploy-{uuid[:12]}` | PO решает задеплоить (reminder, user request) |

### Что произошло

1. Engineering worker завершил CI poll → **Path A** создал `deploy-08a7fa1bf1dd` в 04:02:13
2. Через 25 секунд PO reminder fired → PO проверил task status → увидел что engineering завершён → вызвал `trigger_deploy` → **Path C** создал `deploy-c352c9875cab` в 04:02:42
3. Оба deploy попали в `deploy:queue` и выполнились последовательно
4. Оба упали по BUG 8 (отсутствие `.env.prod`)

**Path B** (webhook) не сработал — project.status был не `active`, guard корректно отфильтровал.

### Почему PO вызвал deploy

PO при создании engineering task ставит reminder на 12 минут (`set_reminder(delay_minutes=12)`). Через 12 минут reminder текст: `"check engineering task eng-08a7fa1bf1dd status for reverse-echo-bot project"`. PO проверяет статус задачи, видит что engineering завершён, но **не проверяет, был ли уже создан deploy task**. PO не имеет информации о том что engineering-worker уже auto-triggered deploy.

### Race window

```
04:02:13 — engineering-worker: deploy_auto_triggered
04:02:38 — PO reminder fired (25 секунд спустя)
04:02:42 — PO: trigger_deploy (не знает про автоматический deploy)
```

Окно гонки: engineering-worker создаёт deploy task, через ~25 секунд PO по reminder'у создаёт ещё один.

### Варианты фикса

**Вариант 1: Дедупликация в PO** — перед вызовом `trigger_deploy` проверять, есть ли уже pending/running deploy task для проекта. PO уже имеет tool `get_task_status`. Можно добавить подсказку в промпт: "перед trigger_deploy проверь нет ли уже deploy задачи".

**Вариант 2: Дедупликация в deploy-worker** — перед обработкой задачи проверить, нет ли уже running deploy для того же project_id. Если есть — skip.

**Вариант 3: Промптовый фикс** — добавить в PO системный промпт правило: "Engineering worker автоматически запускает deploy после CI success. Не вызывай trigger_deploy если engineering task только что завершился — deploy уже запущен автоматически."

**Рекомендация**: Вариант 3 (быстро, достаточно надёжно для текущего масштаба) + Вариант 2 (guard в deploy-worker как safety net).

---

## BUG 8: Missing `.env.prod` — deploy.yml crash

### Суть

GitHub Actions workflow `deploy.yml` падает на шаге "Deploy via SSH" с ошибкой:
```
env file /opt/services/reverse-echo-bot/infra/.env.prod not found:
stat /opt/services/reverse-echo-bot/infra/.env.prod: no such file or directory
```

### Корневая причина

Несоответствие между `compose.prod.yml` и `deploy.yml` — оба генерируются из шаблонов в **service-template**, но не согласованы:

**`compose.prod.yml`** (из `template/infra/compose.prod.yml.jinja`):
```yaml
services:
  backend:
    env_file:
      - ../.env       # основные переменные
      - ./.env.prod   # production overrides  ← ТРЕБУЕТ ЭТОТ ФАЙЛ
  db:
    env_file:
      - ./.env.prod   # ← ТРЕБУЕТ ЭТОТ ФАЙЛ
  tg_bot:
    env_file:
      - ../.env
      - ./.env.prod   # ← ТРЕБУЕТ ЭТОТ ФАЙЛ
  redis:
    env_file:
      - ./.env.prod   # ← ТРЕБУЕТ ЭТОТ ФАЙЛ
```

**`deploy.yml`** (из `template/.github/workflows/deploy.yml.jinja`):
```yaml
# SCP step — копирует ТОЛЬКО compose файлы:
source: "infra/compose.base.yml,infra/compose.prod.yml"
# НЕ копирует infra/.env.prod

# SSH step — создаёт ТОЛЬКО ../.env:
printf '%s' "$DOTENV_B64" | base64 -d > "$PROJECT_DIR/.env"
# НЕ создаёт $PROJECT_DIR/infra/.env.prod
```

### Что такое SCP в deploy.yml

`appleboy/scp-action@v0.1.7` — GitHub Action для копирования файлов с runner'а на удалённый сервер по SCP (Secure Copy Protocol). В нашем случае:
- **source**: `infra/compose.base.yml,infra/compose.prod.yml` из checkout'а репозитория
- **target**: `/opt/services/$PROJECT_NAME/` на deployment сервере
- **overwrite**: true

Это корректно копирует compose файлы, но `.env.prod` не входит в список копируемых файлов, а на deployment сервере он никак не создаётся.

### Замысел `.env.prod`

В service-template есть файл `template/infra/.env.prod.jinja`:
```
# Production-specific environment overrides
# This file is loaded by compose.prod.yml
# Add any production-only settings here
```

Это **пустой placeholder** для production-specific переменных. При scaffold copier рендерит его в `infra/.env.prod` (пустой файл). Идея: `../.env` содержит основной набор переменных, `.env.prod` — только production overrides.

Проблема: при деплое `.env.prod` не переносится на сервер и не создаётся пустым.

### Варианты фикса (в service-template)

**Вариант A: `touch .env.prod` в deploy.yml** — самый простой:
```yaml
# Ensure .env.prod exists (compose.prod.yml references it for overrides)
touch "$PROJECT_DIR/infra/.env.prod"
```

**Вариант B: Добавить `.env.prod` в SCP**:
```yaml
source: "infra/compose.base.yml,infra/compose.prod.yml,infra/.env.prod"
```

**Вариант C: Убрать `.env.prod` из compose.prod.yml** — использовать только `../.env`. Simplest, но ломает возможность production overrides.

**Рекомендация**: Вариант A — добавить `touch` в deploy.yml template. Минимальное изменение, сохраняет архитектурную возможность overrides в будущем.

---

## Какие доработки в каком проекте

### codegen_orchestrator (этот проект)

- **BUG 7 fix**: Дедупликация deploy — промптовый фикс PO + guard в deploy-worker
- Файлы: `services/langgraph/src/po/prompts.py`, `services/langgraph/src/workers/deploy_worker.py`

### service-template (соседний проект)

- **BUG 8 fix**: Добавить `touch "$PROJECT_DIR/infra/.env.prod"` в `template/.github/workflows/deploy.yml.jinja`
- После фикса нужен re-scaffold (или manual patch) существующих проектов

### Связь между проектами

`deploy.yml` и `compose.prod.yml` — шаблоны из service-template. Они рендерятся copier при scaffolding и попадают в сгенерированные проекты. Фикс в service-template автоматически применится ко всем новым проектам. Для уже сгенерированных (reverse-echo-bot) можно вручную добавить `touch` в deploy.yml или перегенерировать.

---

## Итого

| # | Баг | Причина | Проект для фикса | Сложность |
|---|-----|---------|------------------|-----------|
| 7 | Дублирующий deploy | PO reminder не знает про auto-deploy от engineering-worker | codegen_orchestrator | Medium |
| 8 | `.env.prod` not found | deploy.yml не создаёт файл, а compose.prod.yml его требует | service-template | Easy |

Оба бага блокируют деплой. BUG 8 — полный блокер (deploy всегда падает). BUG 7 — приводит к wasted compute и потенциальным race conditions при параллельном деплое.
