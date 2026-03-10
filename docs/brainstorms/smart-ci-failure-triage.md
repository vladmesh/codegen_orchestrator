# Brainstorm: Smart CI Failure Triage — Worker Reject + Failure Classification

> **Дата**: 2026-03-10
> **Контекст**: CI gate тупо ретраит воркеру "почини CI" даже когда проблема инфраструктурная. Воркер не может сказать "это не моя проблема".
> **Status**: done

---

## Current State

### CI Gate (`_ci_gate.py`)
- Уже есть `_is_infra_failure()` — классифицирует по маркерам (docker login, registry, TLS, SSH, deploy)
- При infra failure: пытается rerun workflow 1 раз → если опять fail → возвращает failure
- При code failure: шлёт воркеру "починить" → до 2 попыток
- **Проблема**: классификация неточная. Маркер "deploy" ловит и infra, и code issues. "registry" тоже.

### Worker (Developer Node + Worker Wrapper)
- Воркер получает TASK.md с инструкцией "fix CI"
- Воркер НЕ МОЖЕТ сигнализировать "не могу починить, это не код"
- Если воркер не делает коммит → `no_commit` → task failed → supervisor retry → ещё 3 итерации впустую
- Wrapper возвращает только: `success` + `commit_sha` или `failed` + `error_message`

### Supervisor (task_dispatcher)
- Видит failed task → retry до max_iterations (3)
- Не различает причину: код/инфра/невозможно
- Каждый retry = полный цикл: spawn worker → ждать → CI gate → timeout

### Реальный кейс (e2e 2026-03-10)
1. Worker написал код ✅, lint-and-test passed ✅
2. `build-and-push` упал — Docker login failed (секреты не были записаны = баг оркестратора)
3. CI gate определил infra failure → rerun → опять fail → returned failure
4. Supervisor retry → worker: "уже всё сделано, чистое дерево" → no_commit → fail
5. Ещё 2 retry — то же самое. 6 минут потрачено впустую.

## Problem

**Три разных класса ошибок, одна реакция:**

| Класс | Пример | Правильное действие | Текущее |
|-------|--------|---------------------|---------|
| **Code** | lint fail, test fail, build error | → воркеру на фикс | ✅ Работает |
| **Infra transient** | GH Actions down, network timeout, rate limit | → подождать, rerun | ⚠️ Частично (1 rerun) |
| **Orchestrator/config** | Секреты не записаны, registry недоступен, неправильный Dockerfile из шаблона | → СТОП + алерт админу | ❌ Пытается чинить воркером |

Дополнительно: воркер не может сказать "задача невыполнима" — сейчас это выглядит как "не справился".

## Options Considered

### Option A: Трёхуровневая классификация в CI Gate (маркеры)
Расширить `_is_infra_failure()` до трёх категорий по job name + log markers.
- **(+)** Дёшево, быстро, без LLM
- **(−)** Маркеры хрупкие, не покрывают edge cases
- **(−)** Не решает проблему "воркер не может отказаться"

### Option B: Отдельная LLM Classifier Node (Haiku)
Лёгкая нода перед воркером: CI logs → Haiku → classify.
- **(+)** Точнее маркеров, ~$0.001 за вызов
- **(−)** Ограниченный контекст (только логи, нет кода/спеки)
- **(−)** Ещё одна нода в графе, ещё один контракт

### ~~Option C: Worker Reject Signal~~ → **Выбранный подход (расширенный)**

Изначально рассматривали как "добавить статус `rejected` в wrapper". Но после обсуждения стало ясно: **воркер уже имеет весь контекст** — код, спеки, `gh` CLI, CI логи, историю задач. Он лучший "эксперт" в системе. Не надо строить отдельного классификатора, когда можно просто **правильно попросить воркера**.

## Recommendation: Воркер как CI-диагност

### Ключевая идея

При CI failure — не отправлять воркеру тупую инструкцию "fix CI". Вместо этого создать **специальную CI-fix задачу** с чёткими инструкциями:

> "CI упал. Вот лог. Разберись. Если можешь починить — чини и коммить. Если проблема не в коде (инфра, секреты, оркестратор) — скажи почему, и не делай коммит."

### Два варианта ответа воркера

| Ответ | Сигнал | Реакция pipeline |
|-------|--------|------------------|
| **Починил** | commit_sha != None | CI gate ждёт новый CI run → продолжает |
| **Не моя проблема** | commit_sha = None + structured reason в PROGRESS.md | Pipeline halt + notify admin с reason |

### Архитектура

```
CI failed
  ↓
CI Gate pre-filter (дешёвый, маркеры — как сейчас, но чуть умнее)
  ├─ Очевидно transient (GH down, network) → backoff + rerun, без воркера
  └─ Всё остальное → воркеру как CI-fix task
                        ↓
                  Worker анализирует:
                  - gh run view (логи)
                  - код проекта
                  - спеки, таски
                  - своё понимание что fixable
                        ↓
                  ├─ fixed → commit → CI gate loop continues
                  └─ rejected → reason → halt + admin alert
```

### Что меняется

**1. Worker Wrapper — новый статус `rejected`**

```python
class WorkerResultStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"  # NEW: "не моя задача, вот почему"

class WorkerResult:
    status: WorkerResultStatus
    commit_sha: str | None
    reject_reason: str | None  # populated when status=rejected
```

Как воркер сигнализирует reject: через PROGRESS.md с маркером `## REJECTED` + reason. Wrapper парсит это.

**2. CI Gate — упрощённый pre-filter**

Оставляем `_is_infra_failure()`, но только для очевидных transient cases:
- GH Actions runner unavailable → backoff + rerun (3 раза с 1m, 3m, 5m)
- Network timeout → то же
- Всё остальное → воркеру (включая "registry", "docker login" — пусть воркер разберётся)

**3. Engineering Consumer — обработка `rejected`**

```python
if worker_result.status == "rejected":
    # Не ретраить! Это осознанное решение воркера.
    await mark_task_blocked(task_id, reason=worker_result.reject_reason)
    await notify_admins(
        f"🔧 Worker rejected CI fix for {task_title}:\n{worker_result.reject_reason}"
    )
    # Pipeline halts for this task
```

**4. Task Dispatcher — не ретраит `blocked`**

Новый статус `blocked` (или reuse existing mechanism):
- `failed` → supervisor retry (как сейчас)
- `blocked` → НЕ ретраить, ждать ручного вмешательства

**5. CI-fix TASK.md template**

```markdown
# CI Fix Task

## CI Failure
- **Job**: {job_name}
- **Run URL**: {run_url}
- **Failed step**: {step_name}

## Logs (last 200 lines)
```
{ci_logs}
```

## Instructions
1. Analyze the CI failure above
2. Check if this is a code issue you can fix
3. If YES — fix it, commit, push
4. If NO (infrastructure, missing secrets, orchestrator bug, etc.):
   - Do NOT make any commits
   - Write a `## REJECTED` section in PROGRESS.md explaining:
     - What failed and why
     - Why you can't fix it (e.g. "REGISTRY_PASSWORD secret is empty — this is an orchestrator configuration issue")
     - Suggested action for admin
```

### Почему это лучше отдельного классификатора

1. **Воркер уже оплачен** — он уже запущен для этой задачи (story worker reuse). Не нужен дополнительный LLM вызов.
2. **Полный контекст** — код, спеки, `gh` CLI, git history. Classifier node видит только логи.
3. **Умная диагностика** — воркер может сказать "test fails because the spec says X but scaffold generated Y" — ни маркеры, ни Haiku это не поймут.
4. **Один контракт** — не нужна новая нода, новый граф, новая инфра. Просто расширение существующего wrapper.
5. **Масштабируется** — reject signal полезен не только для CI. Воркер может reject'ить любую задачу ("описание задачи противоречит коду", "зависимость не установлена").

### Поэтапность

**Phase 1 (hotfix, можно сейчас)**:
- CI gate: `lint-and-test` passed + `build-and-push` failed → не слать воркеру, rerun once → halt + notify
- Покрывает конкретный баг из e2e

**Phase 2 (основная работа)**:
- Worker `rejected` status в wrapper
- CI-fix TASK.md template
- Engineering consumer обработка rejected
- Task status `blocked` + admin notification
- CI gate pre-filter для transient (backoff retry)

**Phase 3 (polish)**:
- Dispatcher не ретраит `blocked`
- Admin может через Telegram: "retry" (unblock) или "cancel"
- Метрики: % rejected vs fixed vs retried

## Action Items

- → new task: "Phase 1 — CI gate: lint-and-test pass + build-and-push fail → halt + notify (hotfix)"
- → new task: "Phase 2 — Worker reject signal: REJECTED marker in PROGRESS.md, wrapper parses, new status"
- → new task: "Phase 2 — CI-fix TASK.md template with structured logs and reject instructions"
- → new task: "Phase 2 — Engineering consumer handles rejected status → blocked + admin notify"
- → new task: "Phase 2 — CI gate pre-filter: backoff retry for transient infra failures"
- → idea: "Phase 3 — Admin Telegram commands: /retry, /cancel for blocked tasks"
