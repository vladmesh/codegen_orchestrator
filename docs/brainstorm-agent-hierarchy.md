# Brainstorm: Иерархия агентов и роль LangGraph

> **Дата**: 2026-02-14
> **Контекст**: Обсуждение вызвано конкретным кейсом — `update-framework` был реализован через scaffolder (механический сервис), но по факту требует интеллектуального агента (developer), способного починить ошибки после обновления. Это привело к более широкому обсуждению архитектуры оркестрации.

---

## Проблема текущей архитектуры

### PO — перегруженный центр

PO-воркер сейчас — единственный "умный" агент, через которого проходит всё. У него растёт набор CLI-команд (project, engineering, deploy, update-framework, respond). Каждая новая возможность = новая команда в его промпт. При 20+ командах агент начнёт путаться, выбирать не те инструменты, или игнорировать часть возможностей.

### Механические сервисы не справляются с неожиданностями

Scaffolder — механический: `copier update → commit → push`. Он не может запустить `sync-services`, прогнать тесты, починить если что-то сломалось. Когда задача требует итерации (попробовал → проверил → починил → повторил), нужен агент, а не скрипт.

Конкретный пример: `copier update` обновил шаблон, но `make sync-services` показывает рассинхрон compose-файлов и Dockerfile'ов. Scaffolder не может это починить. Developer-воркер — может.

### Нет разделения ответственности

PO одновременно:
- Общается с пользователем (UX)
- Принимает технические решения (какой action вызвать)
- Знает про infrastructure (deploy, provisioning)
- Управляет жизненным циклом проектов

В реальной команде это 3-4 разные роли.

---

## Идея: Иерархия агентов по ролям

### Аналогия с реальной командой

```
Пользователь (Telegram)
  └── Product Owner — понимает что нужно, общается с пользователем, делегирует
        ├── Tech Lead — декомпозирует техническую задачу, координирует
        │     ├── Developer(s) — пишет код, фиксит баги
        │     ├── Tester — прогоняет тесты, репортит
        │     └── DevOps — деплоит, настраивает инфру
        └── Analyst — уточняет требования, ресёрчит (Phase 3)
```

### Принципы

- **Каждый агент — CLI agent** (Claude Code, Codex, Factory.ai) со своей ролью, инструкциями и набором инструментов.
- **PO не знает про copier, sync-services, Dockerfile'ы.** Он говорит Tech Lead'у: "обнови фреймворк для reverse-bot". Всё.
- **Делегация вниз, отчёт вверх.** PO делегирует Tech Lead'у, Tech Lead делегирует Developer'у. Developer не вызывает PO и не деплоит.
- **Уровни не могут быть перепрыгнуты.** Developer не общается с пользователем. PO не пишет код.

### Что это даёт

- **Фокусированный контекст**: каждый агент — эксперт в узкой области с коротким точным промптом.
- **Масштабируемость**: новый специалист (Security Auditor, DB Migration Agent) = новая роль, а не +20 команд в промпт PO.
- **Параллелизм**: Tech Lead может одновременно запустить Developer на фичу и Tester на регрессию.
- **Устойчивость к ошибкам**: Developer упал → Tech Lead перезапустит или передаст другому, не нужно объяснять от пользователя заново.

### Критика и риски

- **Латентность**: каждый уровень = спаун контейнера + LLM-вызовы. "Поправь тайпо" идёт через PO → Tech Lead → Developer — три хопа.
- **Стоимость**: больше агентов = больше LLM-вызовов. За "обнови фреймворк" платишь за 3 мозга.
- **Испорченный телефон**: "сделай быстрее" → PO: "оптимизация" → Tech Lead: "кэширование" → Developer: закэшировал не то.
- **Отладка**: цепочка из 4 контейнеров, 4 стримов, 4 conversation history. Где сломалось?
- **Over-engineering**: при 1-2 пользователях и 5-10 проектах — иерархия из 5 ролей избыточна.

---

## Роль LangGraph

### Что LangGraph делает хорошо

- **Multi-step workflows с branching**: scaffold → develop → test → retry → deploy
- **State management**: TypedDict state течёт между нодами
- **Tracing**: LangSmith видит каждый шаг, каждое решение
- **Conditional routing**: если тесты упали — retry, если 3 retry — escalate
- **Визуализация**: граф буквально рисуется

### Что LangGraph делает плохо

- **Persistent agents**: PO живёт часами, LangGraph заточен под "запрос → обработка → результат"
- **Real-time communication**: не для стриминга апдейтов пользователю
- **Простые задачи**: один хоп через граф для "передай задачу developer'у" — overhead

### Три варианта позиционирования LangGraph

**Вариант A: LangGraph = единственный оркестратор**

Всё проходит через граф. PO не вызывает ничего напрямую.

```
PO → Redis → LangGraph → { Developer, Tester, DevOps }
```

(+) Полная трассировка, единая точка координации, retry/error handling.
(−) Overhead на простых задачах, граф разрастается, SPOF.

**Вариант B: LangGraph только для сложных workflow**

Простые задачи идут напрямую (PO → Developer), сложные — через граф.

```
PO → Developer (простая задача, напрямую)
PO → LangGraph → {Developer → Tester → Deployer} (сложный workflow)
```

(+) Быстрый путь для простых задач, LangGraph не blocker.
(−) Два пути оркестрации, неконсистентный трейсинг.

**Вариант C (предпочтительный): LangGraph = router + изолированные subgraph'ы**

Всё проходит через LangGraph, но с умной маршрутизацией. Простые задачи = лёгкий subgraph из 1-2 нод. Сложные = полный pipeline.

```
PO → Redis → LangGraph Router:
  ├── update_framework_subgraph: [spawn developer with task] (1 нода)
  ├── engineering_create_subgraph: scaffold → develop → test → deploy
  ├── engineering_fix_subgraph: develop → test → deploy
  └── deploy_subgraph: provision → configure → deploy → verify
```

(+) Единая точка, полный трейсинг, минимальный overhead для простых задач.
(−) Всё ещё зависимость от LangGraph процесса.

### Вопрос: кто такой "Tech Lead" — агент или граф?

**Tech Lead = LangGraph граф (текущий подход)**

Граф сам принимает решения через LLM-ноды. "Tech Lead" — это не контейнер с Claude, а логика маршрутизации и координации внутри графа.

```
LangGraph:
  classify_task(LLM) → "это update-framework"
  plan_execution(LLM) → "нужен 1 developer"
  spawn_developer → wait → check_result → retry/report
```

(+) Простота, один процесс, полный контроль.
(−) Ограничен предопределёнными путями. Непредвиденная ситуация = ступор.

**Tech Lead = CLI agent со своими инструментами**

Полноценный Claude/Codex в контейнере. Сам решает что вызывать.

(+) Гибкость, может обрабатывать edge cases.
(−) Непредсказуемость, трудно отлаживать, может зациклиться.

**Гибрид (предпочтительный вариант)**

Граф определяет высокоуровневый flow. Некоторые ноды — вызовы агентов, которые автономно решают задачу внутри scope.

```
LangGraph:
  → route_task (детерминированная нода)
  → [agent_node: developer решает задачу автономно]
  → check_result (детерминированная нода)
  → decide_next (LLM: retry? deploy? escalate?)
```

---

## Тонкое место: Access Control

### Проблема

Сейчас "иерархия" — это чисто **промпт + набор CLI-команд**. Никакого реального enforcement нет.

Три рубежа "контроля":
1. **Промпт** — "ты developer, не делай deploy". Самый хрупкий, LLM может проигнорировать.
2. **CLI-команды** — orchestrator-cli ставится целиком, все команды доступны всем.
3. **`require_permission`** — проверяет `allowed_commands` в конфиге. Но мы ставим `["*"]`.

Если developer-воркер вызовет `orchestrator deploy trigger` — оно сработает. Иерархия — иллюзия.

### Варианты решения

**Convention-based (текущее)**: промпт + tool availability. Просто, но хрупко. Достаточно на ранних этапах.

**Enforced at system level (будущее)**:
- worker-manager при спауне записывает роль в Redis (`worker:role:{id} = developer`)
- CLI-команды проверяют роль отправителя, а не конфиг-файл
- Redis-очереди валидируют: "developer не может писать в `deploy:queue`"

Это отдельная задача. На текущем этапе convention + правильные промпты достаточны. Но при масштабировании (много пользователей, автономная работа) — нужен enforcement.

---

## Архитектура графов

### Один граф vs несколько

**Один монолитный граф** — становится нечитаемым на 10+ типах задач. Одна бага в routing ломает всё.

**Граф на каждый тип задачи** — изолированные, тестируемые. Но как координировать между ними?

**Предпочтительный вариант: Router + изолированные subgraph'ы**

```
Top-level router:
  → определяет тип задачи (по action в сообщении)
  → запускает нужный subgraph
  → собирает результат
  → уведомляет инициатора

Subgraph'ы (изолированные, каждый тестируется отдельно):
  engineering_create: scaffold → develop → test → deploy
  engineering_fix: develop → test → deploy
  update_framework: develop(copier+sync+lint+test)
  deploy_only: provision → deploy → verify
  ...новые типы добавляются как новые subgraph'ы
```

### Кто может инвоукать граф

**Иерархический доступ** — каждый уровень может вызывать только уровень ниже:
- PO может: engineering, deploy, update-framework
- Tech Lead / граф может: spawn-developer, spawn-tester, run-ci
- Developer может: ничего (только отчитывается)

Рекурсия невозможна by design. Агент не может триггернуть граф, который спаунит агента, который триггерит граф.

---

## Конкретный кейс: update-framework

### Как было (сломано)

```
PO → orchestrator engineering update-framework
  → scaffolder:queue (механический сервис)
  → copier update → commit → push
  → CI падает (sync-services out of sync)
  → никто не чинит
```

### Как должно быть

```
PO → orchestrator engineering trigger --action update-framework
  → engineering:queue → LangGraph router
  → update_framework_subgraph:
    → spawn developer с task:
      "1. copier update --defaults --trust --vcs-ref=HEAD
       2. make sync-services create
       3. make generate-from-spec
       4. make format && make lint
       5. make tests
       6. Если что-то падает — почини
       7. Когда всё зелёное — commit и push"
    → wait for result
    → report to PO
```

Developer — полноценный CLI agent с copier, Docker, git. Может итерировать, фиксить, проверять.

---

## Что нужно для реализации (не план, а направления)

1. **Добавить copier в worker-base-common** — чтобы developer мог запускать `copier update` (уже сделано в этой сессии).

2. **Переделать `update-framework`** — вместо scaffolder:queue отправлять в engineering:queue с `action=update-framework`. Добавить subgraph или route в LangGraph.

3. **Разделить CLI-команды по ролям** — не все команды доступны всем. PO-набор, Developer-набор, DevOps-набор. Как минимум на уровне промптов и инструкций.

4. **Реализовать DockerEventsListener** — без него "иерархия" ломается на первом же убитом контейнере (worker:status протухает, бот шлёт сообщения мёртвому воркеру). См. backlog.

5. **Observability** — без трейсинга через всю цепочку (PO → граф → developer → результат) любая иерархия — чёрная дыра. LangSmith для LangGraph части, структурированные логи + correlation ID для остального.

---

## Открытые вопросы

- **Persistent vs per-task agents**: PO — persistent (один на пользователя). Developer — per-task. Tech Lead — persistent или per-task? Если persistent — нужен отдельный контейнер, session management. Если per-task — overhead на каждую задачу.

- **Стоимость иерархии**: Сколько стоит один уровень делегации в токенах/деньгах? При каком объёме задач это окупается?

- **Fallback при поломке**: Если LangGraph упал — PO не может делегировать. Нужен ли fallback (PO напрямую спаунит developer)?

- **Human escalation**: На каком уровне иерархии агент должен эскалировать к человеку? Только PO → пользователь? Или Tech Lead тоже может?

- **Multi-project coordination**: Один Tech Lead на все проекты пользователя или один на проект?
