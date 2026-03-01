# Brainstorm: Иерархия агентов, роль LangGraph и Incident Response

> **Дата**: 2026-02-14
> **Контекст**: `update-framework` реализован через scaffolder (механический сервис), но требует интеллектуального агента, способного починить ошибки после обновления. Это привело к обсуждению архитектуры оркестрации, структуры общения между агентами и автоматического реагирования на инциденты.

---

## Проблема текущей архитектуры

### PO — перегруженный центр

PO-воркер — единственный "умный" агент. Каждая новая возможность = новая CLI-команда в промпт. При 20+ командах агент начнёт путаться.

**Решение**: единая точка входа `orchestrator task submit --type=X`. Маршрутизация внутри LangGraph, не в голове PO. Промпт не растёт с каждым новым типом задачи.

### Механические сервисы не справляются с неожиданностями

Scaffolder механический: `copier update → commit → push`. Не может прогнать тесты, починить если сломалось. Когда задача требует итерации — нужен агент, а не скрипт. Пример: `copier update` обновил шаблон, но `make sync-services` показывает рассинхрон. Scaffolder не починит, developer-воркер — может.

### Нет разделения ответственности

PO одновременно общается с пользователем, принимает технические решения, знает про infrastructure, управляет жизненным циклом проектов. В реальной команде это 3-4 разные роли.

### Нет реакции на сбои за пределами retry

Когда retry'и исчерпаны — задача помечается FAILED. Никто не анализирует причину, не адаптируется, не уведомляет оператора с контекстом.

---

## Иерархия агентов по ролям

### Целевая модель

```
Пользователь (Telegram)
  └── Product Owner — общается с пользователем, делегирует
        ├── Tech Lead — декомпозирует задачу, координирует
        │     ├── Developer(s) — пишет код, фиксит баги
        │     ├── Tester — прогоняет тесты, репортит
        │     └── DevOps — деплоит, настраивает инфру
        └── Analyst — уточняет требования (Phase 3)
```
(структура ролей просто для примера - на практике может быть проще или сложнеее, будет меняться в процессе экспрериментов)

### Принципы

- Каждый агент — CLI agent со своей ролью, инструкциями и набором инструментов
- Делегация вниз, отчёт вверх. Уровни не перепрыгиваются
- PO не знает про copier, sync-services, Dockerfile'ы — только "обнови фреймворк"

### Критика и риски

- **Латентность**: каждый уровень = спаун контейнера + LLM-вызовы, три хопа на "поправь тайпо"
- **Стоимость**: за "обнови фреймворк" платишь за 3 мозга
- **Испорченный телефон**: "сделай быстрее" → "оптимизация" → "кэширование" → закэшировал не то
- **Over-engineering**: при 1-2 пользователях иерархия из 5 ролей избыточна

### Предпочтительный подход: эволюция, не революция

PO остаётся один, сложность — в subgraph'ах LangGraph. Новый тип задачи = новый subgraph, не новая роль.

```
PO → orchestrator task submit --type=X
       │
       ▼
LangGraph Router (детерминированный, по type)
  ├── create_project_subgraph: scaffold → develop → test → deploy
  ├── fix_bug_subgraph: develop → test
  ├── deploy_subgraph: provision → deploy → verify
  └── add_feature_subgraph: develop → test → deploy
```

Tech Lead как отдельный agent — только когда >15 типов задач и PO путается.

---

## Роль LangGraph

### Сильные стороны

Multi-step workflows, state management (TypedDict), LangSmith tracing, conditional routing, визуализация графа.

### Слабые стороны

Persistent agents (PO живёт часами), real-time communication, overhead на простых задачах.

### Выбранный подход: LangGraph = router + изолированные subgraph'ы

Всё проходит через LangGraph, но с умной маршрутизацией. Простые задачи = лёгкий subgraph из 1-2 нод. Сложные = полный pipeline. Единая точка, полный трейсинг, минимальный overhead.

Типичная структура subgraph'а:

```
route_task (детерминированная) → agent_node (worker автономно) →
check_result (детерминированная) → decide_next (retry? deploy? escalate?)
```

Типы нод: **agent** (CLI-агент в контейнере), **mechanical** (скрипт/сервис), **decision** (LLM/rule-based), **check** (детерминированная валидация). Гетерогенность — сила.

### Кто может инвоукать граф

Иерархический доступ, рекурсия невозможна by design:
- PO может: engineering, deploy, update-framework
- Subgraph может: spawn-developer, spawn-tester, run-ci
- Developer может: request_help, отчитываться

---

## Access Control

### Проблема

Иерархия — иллюзия. `orchestrator-cli` ставится целиком, `allowed_commands = ["*"]`. Developer может вызвать `deploy trigger`.

### Решение (поэтапно)

1. **Сейчас**: convention-based (промпт + tool availability). Хрупко, но достаточно
2. **Следующий шаг**: `allowed_commands` в agent_configs — реальный whitelist. Или subset CLI per role
3. **Будущее**: worker-manager записывает роль в Redis, CLI проверяет роль, очереди валидируют права

---

## Incident Response Pipeline

### Мотивация

Агенты могут решить 90% проблем без человека. Но "дать агентам шариться по системе" — дорого, небезопасно. Нужен pipeline, который реагирует на сбои, анализирует причину, адаптирует систему, помогает застрявшим агентам, эскалирует с готовым отчётом — и при этом не жжёт токены.

### Три слоя с нарастающей стоимостью

```
Ошибка / аномалия / request_help от агента
       │
       ▼
┌─────────────────────────┐
│     Watchdog (service)   │  ← детерминированный, $0, 24/7
│  DLQ, Docker events,     │
│  stuck tasks, health,    │
│  request_help            │
└──────────┬──────────────┘
           │
     Есть playbook?
      ╱          ╲
    Да            Нет
     ▼              ▼
  Executor(fn)   Diagnostician (LLM, read-only, ~$0.03)
                       │
                  ┌────┼────┐
                  ▼    ▼    ▼
              простое  сложное  неясно/critical
                 ▼       ▼         ▼
           Executor  Ops Agent  Incident Report
            (fn)    (≤10 мин)   → Telegram
```

### Слой 0: Watchdog (без LLM)

Расширение `scheduler/health_checker`. Playbooks — детерминированные сценарии:

| Сигнал | Действие |
|--------|----------|
| Container exit code 137 | Перезапуск с увеличенным лимитом |
| Container crash (повторный) | Эскалация к Diagnostician |
| Task RUNNING > 30 min | Kill, mark FAILED, notify |
| GitHub workflow failed | `gh run rerun --failed`, если повторно — эскалация |
| DLQ message | Эскалация к Diagnostician |
| `request_help` от агента | Эскалация к Diagnostician с контекстом |

Покрывает 70-80% инцидентов за $0.

### Слой 1: Diagnostician (LLM, read-only)

Одноразовый LLM-вызов (не agent в контейнере). Read-only доступ: логи, Redis state, Docker, GitHub, API. Выдаёт `{ diagnosis, confidence, action, boundaries }`.

| Confidence | Исполнитель |
|------------|-------------|
| > 0.8, простое действие | Executor-функция |
| > 0.6, нужна итерация | Ops Executor (агент) |
| < 0.6 или CRITICAL | Incident Report → Telegram |

### Слой 2: Executor

**Executor-функция**: whitelist предопределённых действий (restart_worker, retry_github_job, kill_stuck_task, grant_permission, scale_memory, upgrade_model, drain_dlq, notify_admin).

**Ops Executor (агент)**: для случаев, требующих итерации. Тот же механизм, что developer-воркер, но роль ops. Получает INCIDENT.md с диагнозом, рекомендациями и жёсткими boundaries. Время жизни ≤ 10 мин. Не принимает решений — выполняет план Diagnostician'а.

### request_help: агент зовёт на помощь

Не все проблемы — инциденты. Иногда всё зелёное, но задача не решается: прав не хватает, инструмент не установлен, pipeline не учёл edge case.

Tool в orchestrator-cli: `request_help(description, what_tried, what_blocked)`. Публикует в `incident:queue`, Watchdog подхватывает → Diagnostician. **Асинхронный** — агент паузит задачу, ответ приходит в worker input queue.

**Защиты**: max 2 вызова за task; Ops Executor не имеет `request_help` (нет рекурсии); бюджет инцидента ≤ $0.50.

---

## Динамические конфигурации агентов

### Почему полные динамические ноды (как в проекте Assistant) не подходят

В Assistant ноды хранятся в БД. Но: tool types всё равно хардкод в коде (ложная гибкость); ноды в оркестраторе гетерогенны (из 8-10 нод только 2-3 — "LLM + промпт + tools", остальные — механика); дебаг БД-конфигов сложнее чем `git blame`.

### Что имеет смысл: agent_configs в БД

Ноды остаются в коде. **Конфигурации** LLM-agent нод — в БД. Код определяет ЧТО нода делает, БД — КАК (промпт, модель, tools, лимиты).

```
agent_configs:
  id, name, role, system_prompt, model,
  allowed_commands[], container_config (JSONB),
  is_default, project_id (NULL = глобальный)
```

### Killer feature: Incident Response + dynamic configs

Resolver может адаптировать конфигурации на лету — главная причина хранить конфиги в БД.

- **Developer не справляется (слабая модель)**: Diagnostician видит 3 failed retry → Executor создаёт temporary override `model = 'claude-sonnet-4-5'` → respawn → retry
- **Developer'у не хватает инструмента**: Diagnostician оценивает безопасность → Executor добавляет temporary `allowed_commands += ["redis-cli --readonly"]`

### Границы изменений

| Изменение | Кто может |
|-----------|-----------|
| Модель, CLI-команда, container limits | Resolver (temporary, auto-expire) |
| Промпт (мелкие правки) | Ops Executor (temporary) |
| Убрать ограничение, новый тип агента, base image | **Только человек** |

**Temporary** = откатывается после task'а или по `expires_at`.

---


## Shared Session Memory (Контекст между попытками)

### Проблема

LangGraph делает retry, создавая новый чистый контейнер. Агент не знает, почему упал предыдущий (ImportError, timeout, wrong test). Мы просто платим за повторение тех же ошибок.

### Решение

Передавать "предсмертную записку" от упавшего агента к новому через Graph State.

1. **Capture**: При падении (`exit_code != 0`) сохраняем `stderr`, список изменённых файлов и summary попытки.
2. **Inject**: `worker-manager` при создании следующего контейнера (retry) добавляет в `TASK.md` секцию `## Previous Failed Attempt`.
3. **Analyze**: Новый агент видит ошибку и явно инструктируется не повторять её.

Это превращает систему из "стада золотых рыбок" в обучающуюся машину в рамках одной задачи.

---

## Направления реализации

### Базовая инфраструктура (без LLM, покрывает 80%)

1. **DockerEventsListener** — blocker для Watchdog. Уже в бэклоге
2. **DLQ consumer** в scheduler
3. **Watchdog + 5-6 playbooks** — расширение health_checker
4. **Admin Telegram chat** для incident reports
5. **`request_help` tool** в orchestrator-cli
6. **`agent_configs` в БД** — уже частично существует в API

### Интеллектуальная надстройка (когда накопится статистика)

7. **Diagnostician** — LLM read-only, вызывается когда Watchdog не знает что делать
8. **Ops Executor** — agent для сложных инцидентов, scoped write, ≤10 мин
9. **Dynamic config changes** — temporary overrides в agent_configs

### Расширение оркестрации

10. **Переделать `update-framework`** → engineering:queue + subgraph
11. **Единая точка входа** `task submit --type=X` для PO
12. **CLI-команды по ролям** — whitelist вместо `["*"]`
13. **Observability** — LangSmith + structlog + correlation ID

---

### PO без контейнера: переход на API-based ReactAgent

> **Status:** DONE (Implemented as LangGraph API node in `services/langgraph/src/po/graph.py`)

### Проблема

PO-воркер сейчас — Docker-контейнер с Claude Code CLI внутри. Это оправдано для Developer (ему нужна FS, git, build tools). Но PO **не работает с файлами** — он координатор. Весь контейнер существует ради `claude -p "..." --resume X --output-format json` как subprocess.

Путь сообщения: `User → Telegram → Redis → worker-manager → Docker → worker-wrapper → subprocess(claude) → orchestrator-cli → API/Redis → обратно`. 7 хопов для "какой статус у проекта?".

При 20 пользователях — 10-40GB RAM на idle PO-контейнеры. При 100 — система ляжет.

### Предварительное решение: LangGraph ReactAgent через API

PO становится обычным LangGraph графом с `ChatAnthropic` + tool calling. Без контейнера, без subprocess, без worker-manager в PO flow.

```
Было:   Telegram → worker-manager → PO container → orchestrator-cli → API
Будет:  Telegram → LangGraph PO Graph → Direct API/Redis calls
```

**Conversation history** — LangGraph PostgreSQL checkpointer (per-user thread_id). Заменяет Claude Code `--resume`.

**Tools** — прямые Python-функции вместо orchestrator-cli. CLI был нужен как прокси для агента в контейнере. Без контейнера прокси не нужен.

**Модель** — Sonnet (PO — координатор, не кодер). Fallback на Opus для сложных задач. Anthropic primary, OpenAI fallback.

### Стоимость (20 юзеров × 10 сообщений/день)

| Вариант | LLM cost/мес | RAM overhead | Latency |
|---------|-------------|-------------|---------|
| Текущий (Claude Code Max) | $100-200/seat | 10-40GB | 2-10s (subprocess) |
| Sonnet API + prompt caching | ~$50-70 total | ~0 | <2s |
| С model cascade (Haiku для простых) | ~$20-30 total | ~0 | <1s |

### Плюсы

- **Ноль container overhead** — RAM, CPU, Docker daemon не нужны для PO
- **Мгновенный ответ** — нет спауна контейнера, нет subprocess startup
- **Скейлинг** — 100+ юзеров без дополнительных ресурсов (stateless API calls)
- **LangSmith трейсинг** — PO сейчас чёрная коробка, станет полностью прозрачным
- **Гибкие промежуточные ноды** — intent classifier, model cascade, smart routing добавляются как ноды графа
- **Dynamic configs** — модель/промпт из БД, применяются мгновенно (не нужен respawn контейнера)

### Минусы и риски

- **Потеря subscription pricing** — но Sonnet + caching дешевле Claude Code Max
- **Нужно переписать tools** — с CLI-команд на прямые API-вызовы (одноразовая работа)
- **Потеря Claude Code features** — file editing, codebase search. PO они не нужны, но если появится "PO пишет спеку в файл" — придётся решать
- **Vendor lock-in на Anthropic API** — митигация: OpenAI fallback, LangGraph абстрагирует модель
- **Conversation compaction** — нужно самим управлять длиной контекста (суммаризация каждые N сообщений)

### Классификация нод: контейнер vs API

| Нода | Работа с FS? | Рекомендация |
|------|-------------|--------------|
| PO | Нет | **API node** (ReactAgent) |
| Developer | Да (код) | **Container** (как есть) |
| Tester | Да (тесты) | **Container** (как есть) |
| Diagnostician | Нет (read-only) | **API node** (one-shot LLM call) |
| Ops Executor | Возможно | **Container** (если пишет в FS), иначе API |
| Router, checks | Нет | **Functional node** (Python) |
| EnvAnalyzer | Нет | **API node** (уже так) |

Принцип: **контейнер только для нод, которые пишут в файловую систему или запускают произвольный код**. Всё остальное — API или functional nodes.

### Model cascade (оптимизация поверх)

```
User message → Intent Classifier (Haiku/rule-based, ~$0.001)
    ├── "привет" / "статус проекта" → DB query + template ($0)
    ├── "сделай бота" → Sonnet + tools ($0.02-0.05)
    └── "спланируй архитектуру с учётом..." → Opus ($0.10-0.50)
```

80% запросов не требуют дорогой модели. Добавляется как отдельная нода в PO-граф.

### Миграция

1. Реализовать PO как LangGraph граф с tools (прямые API-вызовы)
2. Feature flag: новые юзеры → API PO, старые → container PO
3. Валидация на реальном трафике
4. Deprecate container PO, убрать worker-manager из PO flow
5. `orchestrator-cli` остаётся только для Developer/Tester workers

---

## Открытые вопросы

### С предварительными ответами

- **Persistent vs per-task**: PO — persistent. Developer — per-task. Subgraph'ы stateless. Persistent Tech Lead не нужен.
- **Fallback при поломке LangGraph**: Redis не зависит от LangGraph. Restart — подхватит из stream.
- **Human escalation**: PO → пользователь (бизнес), Incident Pipeline → Telegram (техника). Внутренние агенты не общаются с человеком напрямую.
- **Multi-project**: Один subgraph instance на task, изоляция по correlation_id.

### Без ответа

- **Ops Executor scope**: whitelist операций или sandbox?
- **Playbook expansion**: Diagnostician предлагает новые playbooks или только человек?
- **Incident deduplication**: 5 crash за минуту = 1 инцидент? Нужен debounce.
- **Cross-incident learning**: повторяющийся инцидент → предлагать permanent fix?
- **Budget caps**: макс. бюджет Incident Response за час/день?
