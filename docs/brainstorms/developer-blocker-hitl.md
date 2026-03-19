# Brainstorm: Developer Blocker + Human-in-the-Loop Review

> **Дата**: 2026-03-12
> **Контекст**: Developer не может эскалировать блокеры — молча шипит workaround-ы. Нужен HITL flow: developer говорит "не могу" → задача замораживается → админ разбирается → работа продолжается. Первый шаг к human-in-the-loop в оркестраторе.
> **Status**: draft

---

## Current State

### Что есть

1. **Worker rejection (`## REJECTED`)** — уже работает, но только для инфра-проблем (CI failures, missing secrets, registry auth). Worker пишет `## REJECTED` → task/story → FAILED → admin notified → конец. Это terminal state — работа не продолжается.

2. **TaskStatus.BLOCKED** — существует в enum, но используется только для dependency blocking (`blocked_by_task_id`). Переходы: `IN_DEV → BLOCKED`, `BLOCKED → IN_DEV | BACKLOG`.

3. **Admin notifications** — `notify_admins()` в `shared/notifications.py`. Рассылает в Telegram DM всем `is_admin=True` юзерам. Rate limit 10/hour.

4. **Proactive messages** — `po:proactive` stream → Telegram Bot → юзер получает сообщение.

5. **orchestrator-cli** — у Developer-а есть только `orchestrator respond` для общения. Нет команды "я застрял".

### Что не работает

- Developer скачивает 78 картинок, 56 возвращают 404 → молча создаёт placeholder-ы → шипит "готово" → 72% работы сломано
- Нет статуса "ждём человека" — только FAILED (terminal) или BLOCKED (dependency)
- Нет механизма "разморозить" задачу после human review
- REJECTED → FAILED → story FAILED — слишком грубо, нет пути назад без полного reopen

## Problem

Developer должен иметь возможность сказать "я не могу решить эту задачу" так, чтобы:
1. Работа остановилась (не шипился полурабочий код)
2. Юзер был уведомлён (через PO, не напрямую)
3. Админ получил детали и мог разобраться
4. После review работа могла продолжиться (не terminal state)

Это первый кирпич в human-in-the-loop. Сейчас — админ вручную. Потом — Architect автоматически, PO переформулирует, и т.д.

---

## Proposal: WAITING_HUMAN_REVIEW flow

### Новые статусы

**TaskStatus**: добавить `WAITING_HUMAN_REVIEW`
```
IN_DEV → WAITING_HUMAN_REVIEW   (developer reports blocker)
WAITING_HUMAN_REVIEW → IN_DEV   (admin resumes with guidance)
WAITING_HUMAN_REVIEW → BACKLOG  (admin re-queues with new description)
WAITING_HUMAN_REVIEW → FAILED   (admin decides task is impossible)
WAITING_HUMAN_REVIEW → CANCELLED
```

**StoryStatus**: добавить `WAITING_HUMAN_REVIEW`
```
IN_PROGRESS → WAITING_HUMAN_REVIEW   (when any task enters WHR)
WAITING_HUMAN_REVIEW → IN_PROGRESS   (when admin resolves)
WAITING_HUMAN_REVIEW → FAILED        (admin gives up)
```

### Полный flow

```
1. Developer hits blocker (404 URLs, ambiguous requirements, missing deps)
   │
2. Developer: `orch report-blocker --reason "56/78 Minor Arcana URLs return 404..."`
   │  - Новая команда в orchestrator-cli
   │  - НЕ коммитит код, НЕ пушит
   │  - Worker exits cleanly (exit code 0, status="blocked")
   │
3. worker-wrapper парсит "blocked" status
   │  - Publishes to worker:lifecycle: {status: "blocked", reason: "..."}
   │
4. Engineering consumer:
   │  - Task → WAITING_HUMAN_REVIEW
   │  - TaskEvent(type="blocker", details={reason, worker_id, partial_work_description})
   │  - Story → WAITING_HUMAN_REVIEW (если была IN_PROGRESS)
   │  - Worker container KEPT ALIVE (не удаляется — админ может подключиться)
   │
5. Notifications (параллельно):
   │  a) notify_admins("🔍 Task {id} needs human review: {reason}", level="warning")
   │  b) po:proactive → user: "Задача оказалась сложнее чем ожидалось.
   │     Наш специалист уже подключается к решению."
   │     (PO формулирует user-friendly, не техническое)
   │
6. Admin reviews:
   │  - Видит blocker reason в task events
   │  - Может: SSH в worker container, посмотреть что Developer сделал
   │  - Решает один из вариантов:
   │
   ├─ A) Resume with guidance:
   │     POST /api/tasks/{id}/resume {guidance: "Use picsum.photos instead of Wikimedia"}
   │     → Task: WHR → IN_DEV
   │     → Story: WHR → IN_PROGRESS
   │     → New engineering:queue message с guidance в описании
   │     → Worker получает задачу с доп. инструкциями
   │
   ├─ B) Re-queue with new description:
   │     POST /api/tasks/{id}/requeue {description: "Updated description..."}
   │     → Task: WHR → BACKLOG → TODO (dispatcher picks up)
   │     → Story: WHR → IN_PROGRESS
   │     → Новый worker, свежий контекст
   │
   ├─ C) Fail task:
   │     POST /api/tasks/{id}/fail {reason: "Not feasible"}
   │     → Task: WHR → FAILED
   │     → Supervisor decides: retry/cancel story
   │
   └─ D) Cancel:
         POST /api/tasks/{id}/cancel
         → Normal cancellation flow
```

### orchestrator-cli: report-blocker

Новая команда:
```bash
orch report-blocker --reason "Description of what's blocking"
```

**Что делает**:
1. `POST /api/tasks/{task_id}/events` — создаёт TaskEvent(type="blocker")
2. Записывает файл `/home/worker/BLOCKER.md` с описанием (для последующего review)
3. Выходит с exit code 0, но с результатом `{"status": "blocked", "reason": "..."}`

**Developer prompt addition**:
```
Если задача нерешаема — НЕ пытайся зашипить workaround.
Используй `orch report-blocker --reason "что именно не получается и почему"`.
Примеры когда использовать:
- Внешние ресурсы недоступны (404, timeout)
- Requirements противоречивы или неоднозначны
- Нужны credentials/API keys которых нет
- Задача выходит за scope (нужны изменения в другом сервисе)
```

### API endpoints

```
POST /api/tasks/{id}/report-blocker   — developer reports blocker (via orch CLI)
  Body: {reason: str}
  Effect: task → WHR, story → WHR, notifications

POST /api/tasks/{id}/resume           — admin provides guidance, work continues
  Body: {guidance: str}
  Effect: task → IN_DEV, new engineering message with guidance

POST /api/tasks/{id}/requeue          — admin rewrites task, fresh start
  Body: {description: str | None}
  Effect: task → BACKLOG, story → IN_PROGRESS
```

Reopen (`POST /api/tasks/{id}/reopen`) уже существует — покрывает case B.
`/resume` — новый, для case A (продолжить с guidance).

---

## Эволюция HITL

Это MVP. Дальше можно автоматизировать:

| Phase | Кто решает | Триггер |
|-------|-----------|---------|
| **MVP (сейчас)** | Админ вручную | Telegram notification |
| **Phase 2** | Architect автоматически | BLOCKED task → architect:queue, анализирует blocker, пробует переформулировать |
| **Phase 3** | PO спрашивает юзера | Architect не смог → PO формулирует вопрос юзеру через Telegram |
| **Phase 4** | Self-healing | Паттерны блокеров → автоматические решения (broken URL → fallback source, missing secret → prompt user) |

Каждый phase уменьшает нагрузку на админа. WHR статус и API остаются одинаковыми — меняется только кто вызывает `/resume` или `/requeue`.

---

## Open Questions

1. **Worker container**: Держать живым при WHR или убивать? Живой = админ может зайти и посмотреть, но ест ресурсы. Мёртвый = workspace сохраняется в volume, но контейнер надо пересоздавать.

2. **Частичный коммит**: Если developer сделал 50% работы до блокера — коммитить partial? Или всё откатить? Вариант: developer сам решает — если partial полезен, коммитит перед report-blocker. Если нет — не коммитит.

3. **Timeout на WHR**: Если админ не реагирует 24 часа — автоматически fail? Или ждать бесконечно? Наверное notification escalation (повторное уведомление через N часов).

4. **Множественные блокеры**: Если в стори 3 таска и 2 из них в WHR — стори в WHR. Если админ разрешит один — стори обратно IN_PROGRESS? Или ждать все?

---

## Action Items

- → **task-477f5736**: "HITL MVP: WAITING_HUMAN_REVIEW status + report-blocker + admin resume/requeue" — CREATED
- → idea: "Phase 2: Architect auto-resolves blockers before escalating to admin"
- → idea: "Phase 3: PO asks user for clarification on ambiguous blockers"
- → idea: "WHR timeout escalation: re-notify admin after N hours, auto-fail after 48h"
