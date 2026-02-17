# E2E Investigation: Iteration 11 — Registry TLS + CI Fix Prompt

> **Date**: 2026-02-17
> **Project**: reverse-message-bot (project_id: `9f4556a9`)
> **Branch**: feat/deploy-architecture
> **Status**: Bugs identified, fixes required

---

## Timeline

```
17:02:49 — Services started (make up)
17:03:09 — Provisioner: fire-and-forget для vps-267179, vps-267180
17:03:56 — Provisioner: vps-267179 success (47s, IP 176.223.131.124)
17:04:38 — Provisioner: vps-267180 success (42s, IP 80.209.235.229)
17:05:22 — PO: получил сообщение от пользователя (180 chars)
17:05:25 — PO: создал проект 9f4556a9 (reverse-message-bot), modules: backend, tg_bot
17:05:30 — Engineering: scaffolding triggered, ресурсы аллоцированы (backend:8000, tg_bot:8001 на vps-267179)
17:05:30 — PO: trigger_engineering → eng-381c080af3e9
17:05:32 — PO: set_reminder(12 min)
17:05:41 — Scaffolder: complete (repo created, copier ran, pushed)
17:05:41 — Scaffolder: GitHub secrets set (REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD)
17:05:51 — Worker-manager: image build started
17:06:17 — Worker-manager: image built (26s), container created: worker-dev-reverse-message-bot-3c50cd57
17:06:19 — Claude Code started inside worker
17:11:28 — Claude Code finished (5 min), commit f41d4acc
17:11:28 — Engineering: CI check waiting (ci.yml)
17:11:29 — CI: workflow in_progress
17:13:35 — CI FAILED (run 22108149587) — "Step 'Log in to Docker Registry' failed"
17:13:36 — Engineering: ci_fix_respawn_developer (attempt 1)
17:13:38 — Worker-manager: 2nd container created (worker-dev-reverse-message-bot-118e2308)
17:14:33 — 2nd Claude Code finished (~1 min), commit da06a6dd
17:14:34 — Engineering: CI check waiting (attempt 1)
17:17:12 — CI FAILED AGAIN (run 22108255739) — same "Log in to Docker Registry" error
17:17:13 — Engineering: ci_fix_respawn_developer (attempt 2)
17:17:15 — Worker-manager: 3rd container created (worker-dev-reverse-message-bot-d9f2da31)
17:18:07 — Engineering: CI check waiting (attempt 2)
17:20:59 — Stack stopped (docker compose down), 3rd worker lost Redis connection
```

**Total time**: ~18 min (до принудительной остановки).
**Всё до CI отработало идеально** — provisioning, scaffolding, dev, код написан за 5 мин.

---

## BUG 14: Caddy TLS Handshake Failure (CRITICAL)

### Описание

CI workflow `build-and-push` job не может залогиниться в Docker Registry. GitHub Actions runner получает TLS handshake error при подключении к `5oxt.l.time4vps.cloud:443`.

### Доказательство

`get_workflow_failure_logs()` возвращает:
```
Job 'build-and-push (backend, ., services/backend/Dockerfile, backend)' failed:
  Step 'Log in to Docker Registry' failed
```

Тест с хоста:
```bash
$ curl -v -u "registry:change_me_in_production" "https://5oxt.l.time4vps.cloud/v2/"
* TLSv1.3 (IN), TLS alert, internal error (592):
* OpenSSL/3.0.13: error:0A000438:SSL routines::tlsv1 alert internal error
```

TLS handshake падает до этапа аутентификации — проблема с сертификатом Caddy.

### Конфигурация

**Caddyfile** (`infra/Caddyfile`):
```caddyfile
{$ORCHESTRATOR_HOSTNAME} {
    handle /v2/* {
        basic_auth {
            {$REGISTRY_USER} {$REGISTRY_PASSWORD_HASH}
        }
        reverse_proxy registry:5000
    }
    handle /webhooks/* {
        reverse_proxy api:8000
    }
}
```

**Секреты в `.env`**:
```
ORCHESTRATOR_HOSTNAME=5oxt.l.time4vps.cloud
REGISTRY_USER=registry
REGISTRY_PASSWORD=change_me_in_production
```

### Возможные причины

1. **Caddy не получил сертификат** — Let's Encrypt challenge мог не пройти (DNS, порт 80/443 закрыт, rate limit)
2. **Caddy контейнер не сохраняет сертификаты** — volume для `/data` не настроен, после рестарта теряет сертификат
3. **Hostname не резолвится правильно** — `5oxt.l.time4vps.cloud` резолвится в 109.235.70.139 (это orchestrator машина?), но Caddy может не слушать на 443
4. **Firewall** — порт 443 может быть закрыт для входящих, challenge не проходит

### Предлагаемое исправление

1. Проверить Caddy volume для сертификатов (`/data`)
2. Проверить `docker compose logs caddy` на ACME ошибки
3. Проверить что порты 80 и 443 открыты для Caddy
4. Проверить DNS резолвинг hostname → IP сервера

---

## BUG 15: CI Fix Prompt Hardcoded для Linting (HIGH)

### Описание

Когда CI падает, engineering worker спаунит developer worker для фикса. Промпт для фикса **жёстко заточен под ruff/linting**, даже когда ошибка вообще не связана с кодом.

### Код

**`services/langgraph/src/workers/engineering_worker.py:64-87`**:
```python
task_message = f"""# Task: Fix CI Failures (Attempt {attempt})

## Context

The code was pushed but CI failed. Your job is to fix the issues and push again.

## CI Failure Details

{failure_context or "CI workflow failed. Run `ruff check .` and fix any linting errors."}

## Instructions

1. The repository is already cloned to `/workspace`. Pull latest changes with `git pull`.
2. Run `ruff check .` to see current linting errors
3. Run `ruff format --exclude 'services/**/migrations' --exclude '.venv' .` to auto-format
4. Run `ruff check --fix --exclude 'services/**/migrations' --exclude '.venv' .` to auto-fix
5. For remaining errors that can't be auto-fixed, manually fix them
6. Commit and push your fixes

## Important

- Focus ONLY on fixing the CI failures, do not add new features
- Make a descriptive commit message like "fix: resolve CI linting errors"
"""
```

### Проблемы

1. **Hardcoded linting instructions**: Шаги 2-5 всегда говорят про `ruff check`/`ruff format`, даже когда ошибка — "Docker Registry login failed"
2. **Fallback тоже про linting**: `or "CI workflow failed. Run ruff check . and fix any linting errors."` — дефолт всегда про линтинг
3. **failure_context содержит правильную диагностику**, но инструкции её перетирают — worker читает "Step 'Log in to Docker Registry' failed", а потом видит "Run `ruff check .`"
4. **Бессмысленные ретраи**: На registry/infra ошибки worker 3 раза пушит "fix" коммиты, которые ничего не исправляют. Каждый ретрай = ~1 мин Claude Code + ~2 мин CI = ~3 мин + деньги на API
5. **Нет классификации ошибок**: Все CI failures одинаковые — нет различия между:
   - lint/test failures (developer может починить)
   - infra failures: registry, docker, network (developer НЕ может починить)
   - config failures: missing secrets, wrong env (developer НЕ может починить)

### Как это выглядит в логах

Worker получает задачу:
```
## CI Failure Details

Job 'build-and-push (backend, ., services/backend/Dockerfile, backend)' failed:
  Step 'Log in to Docker Registry' failed

## Instructions

1. Pull latest changes with `git pull`
2. Run `ruff check .` to see current linting errors  ← ???
3. Run `ruff format ...` to auto-format              ← ???
```

Worker честно запускает ruff, ничего не находит, делает пустой коммит или минорное изменение, пушит. CI опять падает на registry.

### Предлагаемое исправление

**Вариант A: Классификация ошибок** (рекомендую):
```python
# Определить тип ошибки по failure_context
INFRA_FAILURES = ["Docker Registry", "Log in to", "docker login", "connection refused"]
if any(marker in failure_context for marker in INFRA_FAILURES):
    # Не спаунить developer — это infra проблема
    logger.error("ci_infra_failure", failure_context=failure_context)
    return False  # Fail fast, notify admin
```

**Вариант B: Адаптивный промпт** (минимальный фикс):
Убрать hardcoded linting инструкции, дать developer'у самому разобраться:
```python
task_message = f"""# Task: Fix CI Failures (Attempt {attempt})

## CI Failure Details

{failure_context}

## Instructions

1. Pull latest changes with `git pull`
2. Analyze the CI failure details above
3. Fix the root cause of the failure
4. Commit and push your fixes
"""
```

---

## BUG 16: Worker Containers Not Cleaned Up After Stack Down (LOW)

### Описание

При `docker compose down` останавливаются core services (Redis, API, etc.), но worker containers (`worker-dev-*`) продолжают работать, т.к. они запущены напрямую через Docker API, а не через compose. Worker'ы попадают в бесконечный цикл reconnect к Redis:

```
Error 111 connecting to localhost:6379. Connect call failed ('127.0.0.1', 6379)
```

### Наблюдение

3 контейнера остались running после `docker compose down`:
```
worker-dev-reverse-message-bot-d9f2da31 Up 4 minutes (healthy)
worker-dev-reverse-message-bot-118e2308 Up 7 minutes (healthy)
worker-dev-reverse-message-bot-3c50cd57 Up 15 minutes (healthy)
```

Потребовали `docker rm -f` для удаления.

### Предлагаемое исправление

Добавить в `Makefile` команду `down` которая также убивает worker'ы:
```makefile
down:
	docker compose down
	docker rm -f $$(docker ps -q --filter "name=worker-dev-") 2>/dev/null || true
```

---

## Что отработало хорошо

1. **Provisioning** — оба сервера (vps-267179, vps-267180) provisioned за ~45 сек каждый, без ошибок
2. **Scaffolding** — repo создан, copier отработал, secrets установлены, pushed — всё за 11 сек
3. **Resource allocation** — порты аллоцированы (backend:8000, tg_bot:8001 на vps-267179)
4. **Developer worker** — Claude Code написал код за 5 мин, commit pushed
5. **CI lint-and-test job** — прошёл (линтинг + тесты), проблема только в build-and-push
6. **get_workflow_failure_logs()** — правильно извлекает job/step name из GitHub API
7. **CI retry loop** — механизм retry корректно работает (attempt tracking, created_after filtering)
8. **Dedup guard (BUG 13 fix)** — не тестировалось в этом E2E (один деплой), но код на месте

---

## Приоритеты

| Bug | Severity | Impact | Fix Location |
|-----|----------|--------|-------------|
| BUG 14 | CRITICAL | Registry недоступен → CI всегда падает → deploy невозможен | infra (Caddy TLS) |
| BUG 15 | HIGH | Бессмысленные ретраи при infra ошибках → потеря времени и денег | `engineering_worker.py` |
| BUG 16 | LOW | Worker containers не чистятся → ресурсы утекают | `Makefile` / worker-manager |

### Рекомендуемый порядок

1. **BUG 14** — починить Caddy TLS (без этого ничего не деплоится)
2. **BUG 15** — добавить классификацию CI ошибок (lint vs infra)
3. **BUG 16** — cleanup workers при stack down
4. Перезапустить E2E для проверки
