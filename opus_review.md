# opus_review — архитектурный аудит codegen_orchestrator

> Отчёт только для чтения. Код, контракты, документация и конфигурация **не изменялись**; этот файл — единственное изменение.
> Язык отчёта — русский; идентификаторы, пути и код приведены дословно.

## 1. Базовая ревизия, резюме и главные находки

| Параметр | Значение |
|---|---|
| Репозиторий | `vladmesh/codegen_orchestrator` |
| Проверенный коммит | `3cb6dceafcd302439ebbe5b392194bf7d0b324be` (`main`, merge PR #674, 2026-09-30 14:09 +0200) |
| Дата аудита | 2026-09-30 |
| Актуализировано по `main` | `64ed0562232d11da9e20e624c2614a17cbf3ba54` (squash PR #687, 2026-10-01 UTC) |
| Метод | 10 параллельных read-only субагентов с явными границами (api; langgraph consumers/clients; langgraph agents/nodes/subgraphs/llm/prompts; scheduler; worker-manager + worker-broker + worker-wrapper; infra-service + scaffolder + telegram_bot + фронтенды; shared; scripts/infra/CI/compose/tests; межсервисные контракты; документация). Итоговая сверка и выборочная ручная перепроверка — автором отчёта. |
| Инструменты | чтение кода, `git grep`, AST-анализ Python 3.13 (длины функций, импорт-граф, карта роутов), `alembic upgrade head` + `compare_metadata` на временном Postgres (вне репозитория), скрипты в scratch-каталоге вне репозитория |

**Обозначения достоверности.** **ФАКТ** — проверено по коду/файлам; **ОЦЕНКА** — производная величина (LOC, токены, эффект); **ГИПОТЕЗА** — вероятно, но требует рантайм-проверки или решения владельца. Номера строк относятся к коммиту `3cb6dce`.

**Оценка токенов:** символы/4 (без реального токенайзера; для Python/Markdown погрешность ±20–25%). «Строк на задачу» — число строк файла(ов), которые агент должен загрузить, чтобы безопасно менять одну ответственность.

### 1.1 Размер системы (ФАКТ, `git ls-files` + `wc`)

| Слой | Строк | Символов | ≈ токенов |
|---|---:|---:|---:|
| Python, не-тесты | 120 608 | 4 686 810 | ~1.17 M |
| Python, тесты | 256 335 | 9 967 066 | ~2.49 M |
| Markdown | 15 564 | 1 017 863 | ~254 k |
| TS/TSX фронтендов | ~7 390 | — | — |

Тестов в 2.1 раза больше, чем кода; из 67 k строк `tests/` около 48 k — `tests/live` (крупнейшие: `tests/live/pipeline_helpers.py` 7 789, `test_harness_contract.py` 6 657, `test_run_evidence.py` 4 772, `run_evidence.py` 4 260). Это отдельный, самый крупный резерв контекста (см. §5.10).

### 1.2 Исполнительное резюме

1. **DONE #687 — главный обязательный документационный контекст сокращён.** После #686 (CHANGELOG 2 525→477 строк) PR #687 разделил `docs/CONTRACTS.md` (~191 k символов) на индекс (~28 k) + 8 тематических guides и изменил AGENTS.md на «индекс + релевантная граница»; CHANGELOG теперь append/search-only, а не default reading. Открытый docs/tooling residual — A21: production logic, исполняемый прямо из Markdown, нужно вынести в `.py/.sh` (§8.4, PR 12).
2. **Гигантские модули** (≥ 900 строк) в каждом сервисе смешивают 4–7 ответственностей; их разрез по уже существующим швам почти не уменьшает LOC, но сокращает объём чтения на типовую правку на 60–90% (ОЦЕНКА, §5).
3. **Доказанное дублирование** остаётся в: (а) каркасе циклов scheduler (~−370 строк), (б) типизированных методах API-клиентов scheduler/langgraph/scaffolder (~11 идентичных методов), (в) «PATCH run → callback → return» в deploy-потребителях, (г) литералах ключей Redis `worker:*` (100+ вхождений без билдера), (д) повторяющихся блоках compose. Четыре расходившиеся копии create-run→publish для deploy-handoff закрыты PR #679. Остаточный безопасный потенциал сокращения остаётся порядка нескольких тысяч строк (ОЦЕНКА, §6).
4. **Мёртвый код** подтверждён в каждом сервисе: ~15 методов API-клиента langgraph, 7 — scheduler, пересылка `worker:events:all → orchestrator:events` без отправителя и читателя, пустой `scripts/agent_configs.yaml` с сидером в 4 местах, ≈10 мёртвых контрактных символов в `shared`, пакет `worker-manager/src/agents/` (§6.6).
5. **Контракты.** Пути HTTP согласованы (все 227 статически разрешимых вызовов клиентов попадают в существующие маршруты). Основной открытый дефект — **нетипизированный вход рабочего потока `worker:{id}:input`**: CONTRACTS.md ссылается на `DeveloperWorkerInput`, который никто не использует; реальная полезная нагрузка — три ad-hoc словаря (§7). Разнобой poison/retry semantics закрыт PR #682: terminal reject теперь идёт DLQ→ACK, transient work имеет bounded redelivery, cancellation пробрасывается наружу; остаётся только механический `StreamCodec` split (§5.8).
6. **DONE #685 — схема БД согласована с моделями, Run type/status проверяются на HTTP- и DB-границах.** Все 14 исходных различий устранены; постоянный `compare_metadata` на мигрированной БД защищает от нового drift. Поведение partial incident indexes и Run CHECK проверяется отдельно (§7.4).
7. **Инцидентные дефекты** (раздел 9): PR #680 закрыл выпуск deploy при неполной записи secrets и снятие чужой deploy-блокировки; PR #679 — deploy-handoff ordering; PR #681 — lifecycle/workspace correctness; PR #682 — poison/redelivery и cancellation semantics; PR #683 — доступ LK bearer к внутренним маршрутам API; PR #685 — произвольные значения Run type/status. Открыты, среди прочего: неработающая очистка очередей проекта и retry Task через промежуточный BACKLOG.

### 1.2.1 Актуализация после семи итераций + предварительного docs cleanup (2026-10-01 UTC)

После исходного аудита в `main` смёржены семь запланированных итераций; отдельно перед PR 7 выполнен предварительный docs cleanup:

- **PR #679**, squash `0c4cec3561c8c0913d9bda1344702b2d71ba149f` — закрыты **A5** и **§9.11**:
  scheduler использует единый deploy-handoff seam для PR-poller, retry, infrastructure-resume и
  user-secret resume; Run получает стабильный id логической попытки и точный `DeployMessage` до
  перехода Story, а queued handoff восстанавливается после прерванной публикации.
- **PR #680**, squash `dd0fc410a9e934dd0bcd8b077319c5e4a2fc2534` — закрыты **§9.1**, **§9.2**
  и **§9.16h**: deploy fail-closed при неполной записи GitHub Actions secrets; deploy-lock получил
  уникальный lease-token и atomic compare-and-delete release; завершённые GitHub Actions runs
  проверяются до teardown cancellation-check, а внешняя отмена run-id классифицируется как
  `WorkflowCancelledError`. PR прошёл полный CI, включая LangGraph service tests и Required CI Gate.
  **§9.16ab** перепроверен и закрыт как не-баг: `deploy.max_deploy_retries` задаёт число допустимых
  deploy failures, поэтому текущая граница `attempts >= max` соответствует контракту и не менялась.
- **PR #681**, squash `d0eb1c8bd81b58257700586674ec8a47aa004386` — закрыты **§9.3**, **§9.5**,
  **§9.16b**, **§9.16c**, worker-manager часть **§9.16d** и **§9.16s**: cleanup worker-streams теперь
  проверяет durable worker ownership вместо одного IDLETIME; workspace GC разделяет project_id/repo_id
  и сканирует worker metadata один раз; Docker event stream переподключается после error/EOF; compose
  timeout chain ограничен единым outer budget; тяжёлые chown/transcript операции вынесены из event loop;
  wrapper переживает git-pull timeout/невалидный UTF-8 и публикует без product hooks. Полный PR CI,
  включая worker-manager/scheduler/langgraph/API service tests и Required CI Gate, прошёл зелёным.
  Механическая декомпозиция `manager.py`/`wrapper.py` намеренно не смешивалась с correctness и
  перенесена в worker-кластер PR 10; Ansible часть §9.16d остаётся в infra/residual PR 12.
- **PR #682**, squash `a5fa185dc021eb177c5d6010086771c34365d9bc` — закрыты **A8**, **§7.2**,
  **§9.6**, **§9.10** и **§9.16i**: общий Redis terminal-reject делает DLQ→ACK только после
  успешного quarantine; reclaiming consumers имеют durable delivery ceiling (default 5), scheduler
  не ACK-ает 429/5xx как успех, infra/langgraph/scaffolder/Telegram poison paths больше не теряют и
  не крутят сообщения бесконечно, notifier перешёл с auto-ACK на manual ACK + PEL reclaim, а
  `CancelledError` снова пробрасывается наружу. Полная CI-матрица, LangGraph service suite и
  Required CI Gate прошли зелёными. Механический `StreamCodec`/split `redis/client.py` сознательно
  не смешан с correctness и перенесён в shared/residual PR 12.
- **PR #683**, squash `1a01b9a3ceeb1ad05c088de5fbb919c8be8d2a77` — закрыт **§9.4**:
  LK bearer после успешной аутентификации допускается только на маршруты, где сам `APIRoute`
  объявляет отдельную bearer-aware owner/admin/current-user dependency; неразмеченные маршруты
  теперь internal-only by default, а `X-Internal-Key` сохраняет сервисную поверхность. Анонимный
  allowlist не расширился. Route-registry regressions фиксируют deny-by-default, а API/LangGraph
  service tests, integration legs и Required CI Gate прошли зелёными.

- **PR #685**, squash `9b9957e9c87fdf9dd8c84aede718e6883f92ff10` — закрыты **A9**, **A10** и **§9.9**:
  ORM описывает оба существующих partial incident indexes и отдельную уникальность Product Brief;
  миграция `b2d4f6a8c0e1` удаляет пять неиспользуемых observation-полей только при пустых/default
  значениях, восстанавливает отсутствующие timestamps и добавляет Run CHECK без смены VARCHAR.
  Run type/status типизированы на входе/выходе API и в фильтрах; явный `status: null` даёт 422,
  пропущенное поле сохраняет прежний статус. Миграция отказывается работать с неизвестными Run
  значениями или данными в удаляемых полях; заполненные даты сохраняются. Service suite проверяет
  нулевой metadata drift, миграцию/rollback и реальные incident predicates/Run CHECK.
  TDD зафиксировал 14 исходных различий до исправления; API service suite — 723 passed.
  [Полный PR CI](https://github.com/vladmesh/codegen_orchestrator/actions/runs/36809385222) и
  [post-merge CI на `main`](https://github.com/vladmesh/codegen_orchestrator/actions/runs/36810123123)
  прошли зелёными, включая Required CI Gate и публикацию проверенных образов.

- **PR #686**, squash `14dd9069558389c4ba89b16363fd1a19381e1ae5` — предварительная часть **A2** перед основной docs-итерацией:
  `docs/CHANGELOG.md` сжат с 2 525 до 477 строк и с 188 524 до 37 617 символов (≈5×); дневная гранулярность
  сохранена за 2026-09-22…2026-10-01, более старая история сведена к крупным продуктовым milestone-записям.
  Старые `### Added/Changed/Fixed` блоки удалены вместе с избыточной детализацией. PR CI #36851047388 прошёл
  Required CI Gate. #686 **не считается отдельной из 12 аудиторских итераций**; его docs-scope был завершён
  основной документационной итерацией #687.

Остальные находки ниже считаются открытыми, если явно не помечены **DONE/CLOSED**. Полный повторный
аудит всего дерева после #687 не выполнялся; актуализация здесь — дельта по семи смёрженным
аудиторским итерациям и отдельному docs-cleanup #686. Исходные размеры, инвентаризация и номера строк сохраняют привязку к `3cb6dce`, если
явно не указана другая ревизия. PR #684 — отдельный workflow hotfix, в счётчик аудита не входит.
Локальная проверка при работе над #685 дополнительно выявила legacy UTC image-GC и изоляцию
wrapper HTTP fixtures (§9.16af/ag); они закреплены за worker-кластером PR 10.

### 1.3 Топ приоритетных находок

| # | Находка | Тип | Приоритет | Разделы |
|---|---|---|---|---|
| T1 | **DONE #680** — deploy fail-closed при неполной записи GitHub-секретов | баг | P0 | 9.1 |
| T2 | **DONE #680** — deploy-lock освобождается только владельцем lease-token | баг | P0 | 9.2 |
| T3 | **DONE #681** — cleanup каналов живого воркера fenced по `worker:meta`, а не только IDLETIME | баг | P0 | 9.3 |
| T4 | **DONE #683** — LK bearer допускается только на явно bearer-aware маршруты; остальное internal-only | безопасность | P0 | 9.4 |
| T5 | **DONE #681** — workspace GC разделяет project_id/repo_id и защищает repo активного воркера | баг | P1 | 9.5 |
| T6 | **DONE #687** — CONTRACTS разделён на индекс + тематические guides; CHANGELOG исключён из default reading; docs drift D1–D5 актуализирован | контекст | P1 | 5.9, 8 |
| T7 | Типизированный `WorkerTurnInput`, удалить мёртвый `developer_worker.py` | контракт | P1 | 7.1 |
| T8 | **DONE #682** — единый terminal reject→DLQ→ACK + bounded delivery для reclaiming consumers | контракт | P1 | 7.2 |
| T9 | **DONE #685** — модели/миграции согласованы; CI сравнивает metadata и проверяет реальные индексы/ограничения | персистентность | P1 | 7.4 |
| T10 | `runtime.periodic_loop` для 10 циклов scheduler (−370 строк) | дублирование | P2 | 6.1 |
| T11 | Общий read-mixin API-клиента (scheduler/langgraph/scaffolder) + типизированный `update_run(RunUpdate)` | дублирование/контракт | P2 | 6.2 |
| T12 | Разрезы: `scheduler/supervisor/deploy.py`, `langgraph/_qa_runner.py`, `worker-manager/manager.py`, `worker-wrapper/wrapper.py`, `api/routers/projects/access.py`, `scripts/check-ci-gate.py` | контекст | P2 | 5 |
| T13 | Удаление подтверждённого мёртвого кода (~1 k строк) | объём | P2 | 6.6 |
| T14 | **DONE #685** — Run type/status ограничены enum на API-границе и CHECK в БД | контракт/баг | P2 | 7.3, 9.9 |

---

## 2. Инвентаризация сервисов и матрица покрытия

### 2.1 Инвентаризация

| Компонент | Код, строк (без тестов) | Тесты, строк | Точки входа | Роль |
|---|---:|---:|---|---|
| `services/api` | 25 374 (из них ~5.25 k миграции) | 36 112 | `src/main.py:37` (FastAPI, глобальная зависимость `require_authenticated_caller`), 28 `include_router` (`main.py:144-173`), `entrypoint.sh` → `alembic upgrade head`; 95 ревизий Alembic, одна голова `a4c6e8f0b2d5` | Состояние Story/Task/Run, допуск/бюджеты, проекты, серверы, LK |
| `services/langgraph` | 28 703 | 56 015 | `python -m src` (PO-потребитель + `provisioner:trigger` + `worker:events:all` + напоминания), `src.consumers.{engineering,deploy,qa,architect}` (отдельные контейнеры одного образа) | PO ReAct-агент, Architect, engineering/devops подграфы, QA |
| `services/scheduler` | 14 255 | 29 852 | `src.pipeline` (10 циклов), `src.infrastructure` (3), `src.maintenance` (3), `src.stand_health_probe` | Циклы надзора и диспетчеризации |
| `services/worker-manager` | 8 695 | 16 317 | `src/main.py:137` FastAPI + lifespan (consumer `worker:commands`, Docker events, 4 GC-задачи) | Жизненный цикл контейнеров-воркеров, compose-прокси |
| `services/worker-broker` | 336 | 324 | `src/main.py:136` FastAPI :8001 | Аутентификация воркера, lease/output/status/session, проброс compose |
| `packages/worker-wrapper` | 8 539 (вкл. тесты пакета) | — | `worker_wrapper/main.py:16` → `WorkerWrapper.run` (`wrapper.py:267`) | Цикл хода внутри образа воркера |
| `services/infra-service` | 3 253 + Ansible | 5 674 | `python -m src.main` (consumer `provisioner:queue`), 3 операторских CLI | Провижининг серверов через Ansible |
| `services/scaffolder` | 1 321 | 2 183 | `src/main.py` → `consumer.main` (`scaffold:queue`) | copier + make setup + git push |
| `services/telegram_bot` | 1 751 | 2 620 | `src/main.py:main` (polling) | Интерфейс Telegram ↔ PO |
| `services/admin-frontend` | ~6.5 k TS | 1 тест-файл | Vite SPA за nginx (`/api/` → api, `/wm-api/` → worker-manager) | Админ-панель |
| `services/user-dashboard` | ~0.9 k TS | — | Vite SPA, `/lk/*` | Кабинет пользователя |
| `shared/` | 21 797 | 24 196 | библиотека + CLI (`python -m shared`, `shared.live_harness_cleanup`, `shared.redis.po_cli`) | Контракты, клиенты, модели ORM, Redis, live-harness |
| `scripts/` | ~22 743 | (часть в `scripts/tests`) | Makefile, CI, стенд | CI-гейт, релиз, стенд, очистка |
| `infra/`, compose, CI | YAML/конфиги | — | `docker-compose{,.prod,.stand}.yml`, `.github/workflows/{ci,deploy,stand-e2e}.yml`, `Makefile` | Инфраструктура, CI/CD |
| `tests/` (корень) | — | ~66 883 | `unit`, `integration`, `live`, `compose`, `fixtures` | Кросс-сервисные и live-тесты |
| Документация | 15 564 строк md | — | `AGENTS.md` (канон), `CLAUDE.md`, `ARCHITECTURE.md`, `docs/*` | — |

### 2.2 Матрица покрытия

| Компонент | Проверенные точки входа / файлы | Глубина | Ограничения |
|---|---|---|---|
| api | `main.py`, `dependencies.py`, все 28 роутеров + `routers/projects/*`, `_story_*`, `_task_*`, `*_admission.py`, `schemas/*`, `migrations/` (голова и сравнение с моделями), таблица 233 маршрутов | полная статическая | рантайм не запускался |
| langgraph consumers/clients | `main.py`, `__main__.py`, `consumers/*` (все), `clients/*`, `events.py`, `worker_events.py`, `allocations.py`, `provisioner.py`, `config/` | полная статическая | — |
| langgraph agents/nodes/subgraphs/llm/prompts | `graph.py`, `subgraphs/engineering.py`, `subgraphs/devops/*`, `agents/{po,architect,qa}/*`, `nodes/*`, `llm/*`, `prompts/*` | полная статическая | поведение LLM не оценивалось |
| scheduler | `pipeline.py`, `infrastructure.py`, `maintenance.py`, `runtime.py`, `startup.py`, все `tasks/*` и `tasks/supervisor/*`, `clients/api.py` | полная статическая | — |
| worker-manager / broker / wrapper | `manager.py`, `consumer.py`, `events.py`, `compose_*`, `garbage_collector.py`, `worker_removal.py`, `routers/*`; broker `main.py`, `auth.py`; wrapper `wrapper.py`, `broker.py`, `http_server.py`, `compose_proxy.py`, `runners/*`; Dockerfile образа воркера | полная статическая | Docker не запускался |
| infra-service | `main.py`, `provisioner/*`, `clients/*`, `nodes/__init__.py`, `ansible/` (playbooks, roles) | полная статическая, Ansible — по ссылкам | плейбуки не выполнялись |
| scaffolder | `consumer.py`, `scaffold.py`, `spec_extractor.py`, `validation.py`, `clients/api.py`, Dockerfile | полная | — |
| telegram_bot | `main.py`, `handlers.py`, `keyboards.py`, `proactive.py`, `middleware.py`, `notifications.py`, `clients/api.py` | полная | — |
| admin-frontend / user-dashboard | `lib/api.ts`, `types/api.ts`, все `pages/*`, `nginx.conf`, сверка полей TS ↔ Pydantic | полная статическая | `npm` не запускался |
| shared | все подмодули; импорт-граф по всему репозиторию (включая `python -m`, `docker exec`, `COPY` в Dockerfile) | полная статическая | — |
| scripts / infra / CI / compose / Makefile / Dockerfiles | все скрипты (по ссылкам), 3 workflow, 3 compose + тестовые compose, Makefile, все Dockerfile*, `pytest.ini`, `pyproject.toml` | полная статическая | make/docker/CI не запускались |
| Корневые `tests/` | структура, fixtures, conftest, списки исключений; крупнейшие файлы — только по размеру | частичная | содержимое `tests/live/*` детально не ревьюилось |
| Документация | все `*.md` в корне, `docs/`, `docs/examples`, `docs/playbooks`, `tests/live/README.md`; 1 009 ссылок на пути/модули проверены скриптом; 30 якорей | полная | — |

**Ручная перепроверка автором отчёта** (выборочно, подтверждено чтением кода): T1 (`deployer.py:695-714`), T2 (`consumers/deploy.py:433-460, 975-1033`), T3 (`queue_cleanup.py:21-28, 55-66`), T4 (`dependencies.py:330-364`, `routers/users.py:204-209`, `main.py:144-173`), T5 (`garbage_collector.py:300-345`, `manager.py:295-301`, `workspace.py:25-35`), 9.6 (`infra-service/src/main.py:388-433`), 9.7 (`teardown.py:274-296`), 9.8 (`scheduler/.../liveness.py:482-488`), 9.10 (`provisioner_result_listener.py:55-75, 118-147`), 9.13 (`telegram_bot/src/keyboards.py:80-84`), 9.14 (`introspect.py:454-461`, `admin-frontend/src/lib/api.ts:20-27`), 9.20 (`shared/redis/client.py:414-416`), `shared/models/incident.py:8`, `scripts/agent_configs.yaml` (`[]`), `Makefile:468-472` vs `:307`, `deploy.yml:423,709`, мёртвый `worker:events:all` (один вхождение в репозитории).

---

## 3. Текущая архитектура и карта межсервисных контрактов

### 3.1 Процессная топология (ФАКТ)

```
Telegram ─► telegram_bot ─XADD po:input─► langgraph (PO consumer) ─XADD po:response:{rid}─► telegram_bot
                                               │ tools → API (HTTP, shared/clients/internal_api.py)
                                               ├─XADD architect:queue─► architect (образ langgraph)
                                               └─XADD po:proactive─► telegram_bot

scheduler-pipeline (10 циклов) ─► scaffold:queue ─► scaffolder
                                ─► engineering:queue ─► engineering-worker ─► worker:commands ─► worker-manager
                                ─► deploy:queue ─► deploy-worker ─► GitHub Actions deploy.yml
                                ─► qa:queue ─► qa-worker
scheduler-infrastructure ─pub/sub provisioner:trigger─► langgraph ─► provisioner:queue ─► infra-service ─► provisioner:results ─► scheduler, telegram_bot
worker-manager ◄─► worker-broker ◄─► worker-wrapper (в контейнере воркера): worker:{id}:input / worker:{id}:output
admin-frontend ─nginx─► api, worker-manager ; user-dashboard ─nginx─► api /lk/*
api ─► PostgreSQL (единственный владелец ORM-моделей; langgraph отдельно использует checkpoint-схему)
```

### 3.2 Карта Redis-контрактов (ФАКТ; подробности — §7)

| Поток/канал | Производители | Потребитель, модель разбора | Отклонения |
|---|---|---|---|
| `scaffold:queue` | scheduler `scaffold_trigger.py:118,179` `ScaffoldMessage` | scaffolder `consumer.py:434`, `ScaffoldMessage` | невалидное → ACK без DLQ; поле `telegram_chat_id` не читается |
| `architect:queue` | API `stories.py:1180`; PO `tools_stories.py:289,415`; scheduler `story_completion.py:108`, `liveness.py:177,251` | `architect.py:1169`, `ArchitectMessage` | CONTRACTS.md:1071 не называет scheduler |
| `engineering:queue` | API `_task_actions.py:640`; scheduler `task_dispatcher.py:386`, `supervisor/deploy.py:806` | `engineering.py:500` | CONTRACTS.md:1072 не называет API |
| `deploy:queue` | API (7 мест), langgraph `engineering_result_handler.py:744`, scheduler (6 мест) | `deploy.py:1038` | CONTRACTS.md:1073 не называет langgraph |
| `qa:queue` | API `applications.py:679`, scheduler `handoff.py:83`, `temporary_access.py:390` | `qa.py:1147` | не очищается при удалении проекта |
| `provisioner:queue` | API `servers.py:885`, langgraph `provisioner_client.py:44` | infra-service `main.py:451` | невалидное — вечный повтор (9.6) |
| `provisioner:results` | infra-service `main.py:110,153` (строковый литерал) | scheduler, telegram_bot (`auto_ack=True`) | см. 7.2 |
| `worker:commands` | langgraph (7 мест), scheduler (3), worker-manager `manager.py:392` | worker-manager `consume_typed(WorkerCommand)` | `StatusWorkerCommand` не производится |
| `worker:responses:developer` | worker-manager `consumer.py:204` | langgraph `worker_spawner.py:393` как dict | ответы не валидируются |
| `worker:{id}:input` | langgraph `worker_spawner.py:714-729, 1066-1083`, `qa_worker.py:253` — **ad-hoc dict** | broker `main.py:183` → wrapper `data.get(...)` | нет контракта (7.1) |
| `worker:{id}:output` | broker `main.py:216` (`WorkerResult`), worker-manager `events.py:140-145` (без `request_id`) | langgraph `worker_spawner.py:348` | — |
| `po:input`, `po:proactive`, `po:response:{rid}` | см. §7 | PO `consume_typed` (DLQ ✔), бот | `po:response` без константы (7 написаний) |
| `orchestrator:events` | langgraph `events.py:36` через `worker_events.py:51` | **нет** | мёртвый путь |
| pub/sub `provisioner:trigger` | scheduler `provisioner_trigger.py:64` | langgraph `provisioner.py:37` | с потерями; имя в двух модулях |
| `callback_stream` | langgraph `_events.py:57` | — | **никто не задаёт `callback_stream`** — путь мёртв |

### 3.3 HTTP-контракты (ФАКТ)

Единый транспорт — `shared/clients/internal_api.py`. Все 227 статически разрешимых вызовов клиентов (scheduler, langgraph, scaffolder, infra-service, telegram_bot, worker-manager, shared) совпадают с маршрутом и методом API; динамические `stories/{id}/{action}` используют существующие действия. worker-wrapper ↔ broker ↔ worker-manager также согласованы. Фронтенды: все 60+ вызываемых путей существуют. Проблемы — дублирование клиентских методов (§6.2) и нетипизированные тела PATCH (§7.3).

### 3.4 Оценка соответствия лучшим практикам

- **Сильные стороны (ФАКТ):** единая точка входа в API и глобальный гейт аутентификации с тестом по `app.routes`; типизированные DTO для большинства очередей; PEL-восстановление и live-work lease с fenced XACK в `_base.run_queue_worker`/`_live_work.py`; детерминированные id QA-run; тесты-гейты фронтенд-контрактов и платформенного манифеста.
- **Слабые стороны:** доменная логика API живёт внутри пакета `routers` и импортируется из admission-модулей через 20 функционально-локальных импортов, чтобы обойти цикл (`engineering_dispatch_admission.py:693-695`) — нарушение слоистости; `agents/qa/tools.py:61-62` и `agents/po/situation.py:49` импортируют приватные модули `consumers._*`; generic `RedisStreamClient` ветвится по `is_po_stream` в 9 местах (`shared/redis/client.py:213…668`); `shared.models` (ORM, 1 583 строки) подтягивается scheduler ради одного enum (`app_health_prober.py:16`, `health_checker.py:20`).

---

## 4. Таблица находок (архитектура, дублирование, контракты, документация)

Баги и неэффективности вынесены отдельно в §9.

| ID | Приоритет | Находка | Доказательство | Сервисы | Влияние | Риск изменения | Трудозатраты |
|---|---|---|---|---|---|---|---|
| A1 | P1 | CONTRACTS.md обязательна к полному чтению (47 k ток.), из которых ~42 k — фичевые инварианты | `AGENTS.md:43`; секции CONTRACTS.md 27-151, 152-311, 351-643, 644-1044, 1351-1521, 1729-1883, 2464-2649 | все | −35 k ток. на задачу | тесты читают CONTRACTS (`test_template_pin_single_source.py:29-33`, `test_architect_prompt.py:342`), якоря `#consumer-patterns` | M |
| A2 | **PARTIAL #686** | CHANGELOG исторически был 46.9 k ток. и приглашался Navigation как «что уже сделано». #686 сжал его 2 525→477 строк (~9.4 k ток. по прежней оценке) и удалил старый `### Added/...` формат; осталось убрать его из подразумеваемого полного чтения в `AGENTS.md` | `AGENTS.md:9-20`; PR #686 | все | ещё ~9.4 k ток. при ненужном полном чтении | allowlist пина в тесте | XS |
| A3 | P2 | 10 копий каркаса цикла scheduler | §6.1 | scheduler | −370 строк | имена событий логов в тестах | S |
| A4 | P2 | ~11 идентичных типизированных методов API-клиента в 2–3 сервисах; 28–29 сырых `api_client.patch(f"runs/…")` | §6.2 | scheduler, langgraph, scaffolder | −60 строк/сервис, единая типизация | patch-таргеты тестов | M |
| A5 | **DONE #679** | Единый recoverable deploy handoff вместо 4 расходившихся create-run→publish путей | `services/scheduler/src/tasks/deploy_dispatch.py`; PR #679 | scheduler | стабильные attempt id, точный message в Run, единый порядок | закрыто | — |
| A6 | P2 | Доменные хелперы API внутри `routers/` + 20 локальных импортов из admission-модулей | `engineering_dispatch_admission.py:181,199-201,281-285,328,391,480,506-508,590,696,853-854`; `infrastructure_park.py:103-104` | api | снятие цикла; разрез файлов становится возможен | patch-таргеты | M |
| A7 | P1 | Вход `worker:{id}:input` без контракта; CONTRACTS ссылается на мёртвые `DeveloperWorkerInput/Output` | §7.1 | langgraph, broker, wrapper, shared | «Contracts first» нарушен | Review Trigger (`shared/contracts`) | M |
| A8 | **DONE #682** | Общий terminal reject→DLQ→ACK, bounded delivery и cancellation propagation | §7.2; PR #682 | все потребители | poison не теряется и не крутится бесконечно | закрыто | — |
| A9 | P1 | **DONE #685** — 14 различий моделей/миграций устранены; guarded migration + постоянная проверка schema drift | §7.4; миграция `b2d4f6a8c0e1`; `test_schema_metadata.py` | api, shared | защитные индексы сохранены; даты backfilled без потери известных значений | guard останавливает миграцию при несовместимых данных | закрыто |
| A10 | P2 | **DONE #685** — `RunType`/`RunStatus` в API schemas, shared `RunCreate` и фильтрах; Run CHECK в БД | `services/api/src/schemas/run.py`; `shared/contracts/dto/run.py`; `shared/models/run.py` | api, langgraph, scheduler | invalid type/status и explicit null status → 422 до записи | VARCHAR и допустимые persisted values сохранены | закрыто |
| A11 | P2 | `shared.models` тянется в scheduler ради `IncidentType` | `scheduler/src/tasks/app_health_prober.py:16`, `health_checker.py:20`; `shared/models/incident.py:8` | scheduler, shared | блокирует перенос ORM в api (−1 583 строк из shared) | нулевой | XS |
| A12 | P2 | Литералы ключей Redis `worker:status/meta`, `workspace:lock`, `worker:{id}:input/output`, `po:response:{rid}` без билдеров (100+ вхождений) | §6.5 | langgraph, scheduler, broker, manager, bot | рассинхронизация при опечатке | нулевой (те же строки) | S |
| A13 | P2 | Легаси-базовые классы «узлов» без использования (`RetryPolicy`, `FunctionalNode`, `timeout_seconds`) в двух сервисах | `langgraph/src/nodes/base.py:33,47`; `infra-service/src/nodes/__init__.py` (116 строк) | langgraph, infra | −85 строк | низкий | XS |
| A14 | P2 | Одноузловой LangGraph с `MemorySaver` для провижининга (+ утечка памяти) | `langgraph/src/graph.py:41-55`; `provisioner.py:26,114-127` | langgraph | −50…−330 строк | удаление моста меняет поведение | S/M |
| A15 | P3 | `admin-frontend/src/types/api.ts` (719 строк) пишется вручную; гейт покрывает 5 страниц | `services/api/tests/unit/test_admin_frontend_contract_gate.py` | admin-frontend, api | −500 строк рукописных типов | нужен build-шаг | M |
| A16 | P3 | Compose: `db` ×4, `redis` ×6 идентичных блоков в тестовых файлах; 5 сервисов образа langgraph и 3 scheduler повторяют ключи | §6.7 | infra/CI | −220 строк | `check-ci-gate` не понимает `extends` | S |
| A17 | P3 | Тройной список исключений offline-live, уже разошедшийся | `Makefile:340-356`, `scripts/test-unit-local.sh:126`, `scripts/check-ci-gate.py:300-315` | CI | дрейф | низкий | XS |
| A18 | P3 | Промпты: дублирование правил `present_product_brief` в промпте и docstring (≈400–500 ток. на каждый вызов PO) | `prompts/po/__init__.py:141-164` vs `agents/po/tools_briefs.py:285-338` | langgraph | −650–750 ток./вызов (с A18b) | формулировки закреплены тестами | S |
| A19 | P3 | Настройки с дефолтами для connectivity в worker-manager/worker-broker вопреки AGENTS.md | `worker-manager/src/config.py:9-10,22-23`; `worker-broker/src/config.py:6,10` | worker-* | политика fail-fast | средний (compose задаёт значения) | XS |
| A20 | P3 | Тот же параметр под разными именами (`BROKER_INTERNAL_TOKEN` vs `WORKER_BROKER_INTERNAL_TOKEN`; `TIME4VPS_LOGIN`/`TIME4VPS_USERNAME`; `SCAFFOLDED_WORKSPACE_PATH`/`WORKSPACE_BASE_PATH`) | §7.6 | несколько | путаница | низкий | XS |
| A21 | P2 | **Executable docs as source code**: unit tests читают Markdown-runbook, извлекают Python/bash и исполняют/парсят его (`exec(ast.parse(...))`, regex по fenced blocks). Перенос prose ломает кодовые тесты, а семантическое протухание окружающего текста всё равно не проверяется | `services/scheduler/tests/unit/test_po_maintenance_preflight.py`, `tests/unit/test_backup_rootless.py`; обнаружено при #687 | docs, scheduler, CI | скрытая связность docs↔code; лишний test LOC; документальные рефакторинги становятся рискованными | низкий/средний | S |

---

## 5. Предложения по декомпозиции гигантских файлов/классов/функций

**Методика оценки контекста.** «До» — строки файла, которые агент загружает целиком для правки одной ответственности; «после» — строки целевого модуля + его интерфейсы/импорты. Токены ≈ строки × 10 (ОЦЕНКА по средним 38–42 символам на строку в этих файлах). Разрез модуля сам по себе **не уменьшает общий LOC** (чаще +1–2% на импорты) — выигрыш даёт одновременная дедупликация, указанная отдельно.

**Общие ограничения для всех разрезов (ФАКТ):**
- Тесты патчат атрибуты модулей по строковому пути: например `src.consumers.qa.run_qa_centrally` (53×), `src.consumers.engineering_result_handler.api_client` (32×), `subgraphs.devops.deployer.GitHubAppClient` (54×), `src.manager.workspace_mod` (46×), `worker_wrapper.wrapper.WORKSPACE_DIR` (23×). Перенос функции переносит patch-таргет. AGENTS.md запрещает compatibility-шимы, поэтому вместо реэкспортов нужен механический codemod тестов в том же PR.
- Порядок операций, маркеры `live_work_settled/unsettled`, намеренные `raise`, оставляющие запись не-ACK (`StoryStopError`, `EmptyResultSettlementError`, `WorkflowCancellationUnprovenError` и др.), переносятся дословно.

### 5.1 `services/scheduler/src/tasks/supervisor/deploy.py` (1 750 строк)

- **Текущие границы (ФАКТ):** enum/наборы исходов 123-233; маршрутизация `supervise_deploying_stories`/`_supervise_deploying_story`/`_route_deploy_outcome` 236-405; recovery-handoff 408-493; `_handle_deploy_success_story` 496-674 (179 строк); code-fix/retry/settings-seed/redispatch 677-1162; refused/infra-wait/user-secret 1165-1745.
- **Разрез:** `deploy_routing.py` (~380, вход машины состояний), `deploy_success.py` (~180; выделить `_qa_handoff_preconditions(...) -> (project, repo, initiating_run_id) | FailureReason` из 524-603), `deploy_fix_retry.py` (~480), `deploy_waits.py` (~580). `supervisor/__init__.py` сохраняет экспорт.
- **Инварианты:** счётчики `deploy:retries:*`, детерминированный `_qa_run_id_for_deploy`, фенсы `expected_execution_run_id` — переносятся без изменений.
- **LOC:** 0 от разреза; −120 вместе с §6.3/§6.4 (`publish_deploy`, `_fail_and_alert`) (ОЦЕНКА).
- **Контекст:** правка QA-handoff 1 750 → ~180 строк (~17.5 k → ~2 k ток., −90%).

### 5.2 `services/scheduler/src/tasks/pr_poller.py` (1 148 строк)

- **Границы:** образы 132-282, timeline 285-394, CI-сбои 397-540 и 1058-1148, слияние 543-862, `poll_merged_prs` 865-1055 (191 строка — самая длинная функция scheduler).
- **Разрез:** `pr_merge.py`, `pr_images.py`, `ci_failures.py`, `pr_poller.py` (вход). Внутри `poll_merged_prs` — `_route_merged_story(...)` (885-1053) и общий `publish_deploy` (§6.3).
- **Дедупликация:** frozen `_PRContext(story_id, project_id, owner, repo_name, pr_number, log)` вместо 6–7 повторяющихся kwargs в 6 вызовах `_park_story_for_merge_refusal` (`667-757`, `786-822`) — −60 строк (ОЦЕНКА).
- **Контекст:** правка CI-маршрутизации 1 148 → ~250 строк.

### 5.3 `services/langgraph/src/consumers/_qa_runner.py` (1 916 строк — крупнейший файл сервисов)

- **Границы (ФАКТ):** конфиг/попытки 112-253; `QAResult` + разбор/валидация 257-547; health-проверки 550-642; состояние контейнеров 646-812; активация пакета + приёмка, вкл. `_behaviour_row` (120 строк) 816-1300; факты/preflight 1303-1449; вызов исполнителя и оркестрация `run_qa_centrally` (223 строки) 1452-1916.
- **Разрез:** `_qa_result.py` (~290), `_qa_checks_health.py` (~95), `_qa_checks_container.py` (~170), `_qa_checks_package.py` (~485), `_qa_facts.py` (~150), `_qa_runner.py` (~600). `qa.py` импортирует 10 имён (`qa.py:63-74`) — обновить импорты.
- **LOC:** 0. **Контекст:** правка приёмки пакета 1 916 → ~500 строк (−74%). **Риск:** низкий, в основном чистые функции; `finally` с evidence/cleanup остаётся в `run_qa_centrally`.

### 5.4 `services/langgraph/src/consumers/architect.py` (1 178), `qa.py` (1 160), `deploy.py` (1 046), `clients/worker_spawner.py` (1 162)

| Файл | Разрез (строки → модуль) | LOC | Контекст |
|---|---|---|---|
| `architect.py` | 117-324 → `planning_attempt.py`; 327-452 → `returned_notice.py`; 455-642 → `planning_outcome.py`; 645-814 → `briefing.py` (текст промптов; уместнее в `agents/architect/`); 817-914 → `scaffold_wait.py`; вход ~260 | 0 | правка промпта 1 178 → ~170 |
| `qa.py` | 113-485 → `_qa_resolution.py`; 874-1121 → `_qa_settlement.py`; остаток ~450 | −55 (слияние двух `_update_run` в `_handle_qa_fail` 1029-1049/1051-1071 и передача `QAResult` вместо 9 kwargs 810-844) | 1 160 → ~370/250 |
| `deploy.py` | 475-558 + 82-158 → `deploy_access.py`; allocation/redundancy/precheck → `deploy_prepare.py`; `_handle_lifecycle_action` → существующий `deploy_lifecycle.py`; `_route_deploy_result` → рядом с `deploy_result_handler.py`; вход ~200 | −90 (с §6.2.2) | 1 046 → ~200–420 |
| `worker_spawner.py` | 65-254 → `worker_result.py`; 257-499 → `worker_wait.py` (публичные `wait_for_response`/`wait_until_ready`, т.к. `qa_worker.py:50` импортирует приватные `_wait_*`); 502-753 → `worker_turns.py`; остаток ~500 | −60 (§6.5) | 1 162 → ~200–500 |

**Инварианты:** освобождение heartbeat планирования и порядок «release до report» (`architect.py:149-166, 1125-1129`); `reset_task_chain()` (`architect.py:1029`) безопасен только при 1 слоте; порядок «группа ответов создаётся с `$` до команды» (`worker_spawner.py:841-845`, `918-921`); освобождение inflight-ключа в `finally` `qa.py:869-871`.

### 5.5 `services/langgraph/src/subgraphs/devops/deployer.py` (871) и соседние

- **Границы:** ошибки + `_require_live_lease` 37-101; хелперы записи/кодирования/секретов 104-233; диспетчеризация GitHub Actions 242-527; поиск образов/серверов 529-585; `run`/`_deploy` (257 строк)/`_workflow_failure` 587-871.
- **Разрез:** `deploy_errors.py` (~70); `deploy_dispatch.py` — класс, держащий `github, owner, repo, run_id, redaction secrets` (убирает 6–8 повторяющихся параметров); в `deployer.py` — `_deploy` → `_preflight`(620-678), `_write_payload`(680-714), `_fence`(716-743), `_dispatch`(745-778), `_record_success`(780-837).
- **Дедупликация:** `_refused(e, log_event, prefix)` вместо 4 почти одинаковых refusal-словарей (728-743, 763-778, 842-853, 864-871, каждый трижды зовёт `redact_diagnostic`); недостижимая проверка 638-642 (`backend_allocation` уже бросает, `secret_resolver.py:421-441`); повтор комментария 827-829 = 807-809. −45…−60 строк (ОЦЕНКА).
- **Риск:** 54 патча `...deployer.GitHubAppClient` и 49 `...deployer.api_client` — клиенты должны либо передаваться параметром, либо искаться в `deployer.py`, иначе ~100 патчей тихо перестанут действовать.
- **Связанное:** `secret_resolver.py` (576) — `backend_allocation/backend_base_url/backend_http_url` (55-148) → `devops/endpoints.py` (их используют deployer и smoke), derived-keys (445-560) → `derived_keys.py`; `llm/cli_turn.py` (761) → `cli_turn.py`/`cli_codex.py`/`cli_claude.py` (только контекст); `nodes/developer.py` (669) — общий `_result_base(...)` для 4–5 повторов ключей (`297-307`, `461-561`) и слияние трёх веток `_worker_observability` (570-634), удаление test-only обёрток 655-666: −60…−70 строк.

### 5.6 `services/worker-manager/src/manager.py` (1 398) и `packages/worker-wrapper/src/worker_wrapper/wrapper.py` (1 647)

**manager.py:**

| Модуль | Строки | Интерфейс | ~Строк после | Критичный инвариант |
|---|---|---|---|---|
| `broker_registration.py` | 109-148, 1232-1263 | `register`, `unregister`, `build_container_env` | ~70 | регистрация до старта контейнера |
| `workspace_fence.py` | 261-338, 730-751, 1149-1230 | `acquire`, `release`, `find_developer_workspace` | ~190 | `worker:meta` записывается до `SADD workspace:active_projects` (285-301) |
| `creation_validation.py` | 159-187, 658-725, 1101-1147 | чистые функции | ~150 | типы исключений `EngineeringWorkerCreationRefusal` |
| `creation_state.py` | 340-399 | `reject`, `fail_acquired` | ~55 | enqueue delete-команды |
| `worker_materials.py` | 1021-1070, 1265-1386 | один chunked-писатель файлов | ~110 (−50) | порядок checkout → materials → QA probe → RUNNING (984-996) |
| `manager.py` | остаток | фасады, оркестрация | ~430 | `create_worker_with_capabilities` (266 строк) → `_admit()` (805-868) + `_provision()` (876-998) |

Контекст: правка инъекции 1 398 → ~110–190 строк. LOC: −60…−90 от дедупликаций (D1, D11 ниже).

**wrapper.py:** `codex_lock.py` (37-65), `agent_env.py` (116-215), `git_publish.py` (493-685, 1001-1050), `venv_paths.py` (777-968, три цикла glob+regex → один `_rewrite`), `agent_process.py` (1052-1307, 1413-1441), `workspace_files.py` (735-776, 1017-1030, 1547-1613), ядро ~480. **Ограничение (ФАКТ):** образ воркера копирует только `shared/contracts`, `shared/constants.py`, `shared/log_config`, `shared/diagnostics.py` (`images/worker-base-common/Dockerfile:67-70`) — новые модули только внутри `worker_wrapper`. **Предусловие:** сначала сделать `WORKSPACE_DIR`/`TASK_MD_PATH` атрибутами экземпляра, иначе 23+9 патчей тихо перестанут действовать. LOC −70…−100.

### 5.7 `services/api` — крупнейшие роутеры

| Файл | Текущие границы | Разрез | LOC | Контекст |
|---|---|---|---|---|
| `routers/projects/access.py` (1 211; `_lifecycle` 223 строки) | предикаты evidence 255-416, 544-613; политика исчерпания/повторов 167-252, 419-541; движок 616-941; маршруты 944-1211 | `projects/_grant_evidence.py`, `projects/_grant_lifecycle.py` (+ шаги `_load_or_create_intent` 637-666, `_require_expected_execution` 668-682, `_gate_automatic_rebind` 683-699, `_rebind_target` 701-719, `_open_retry_epoch` 725-788, `_mint_grant_run` 801-838), `access.py` — маршруты | −30 (`_stage(kind)` для `grant_user`/`transfer_ownership` 944-994) | маршрут: 1 211 → ~330 |
| `routers/stories.py` (1 183) | CRUD/уведомления 129-420; completion 426-637; recheck/accept 640-780, 854-1022; переходы 783-851, 1025-1135 | `_story_completion.py`, `_story_recheck.py`, `stories.py` | −35 (`_simple_transition` для четырёх одинаковых `pr_review/deploy/test/archive` 1049-1135, декораторы сохранить) | recheck: 1 183 → ~330 |
| `routers/_story_actions.py` (979) | PR-conflict 110-358; infra retry/park 389-715; user-secret/state-wait 722-922 | слить PR-conflict с `_pr_conflict_attempt.py`; `_infrastructure_actions.py`; слить wait-действия с `_resource_wait_actions.py` | 0 | ~250–330 на тему |
| `routers/servers.py` (1 022) | provisioning 68-98, 183-353, 871-886; ports 357-455; readiness 458-594, 713-801; monitoring 913-1022 | пакет `servers/` | −10 (§6.8 порт) | ~200–300 |
| `routers/runs.py` (973; `update_run` 157) | accounting 83-189; dispatch 761-973 | `_run_accounting.py`, `_run_dispatch.py`; чистая `_validated_run_update(...)` из 616-713 | −2 (мёртвое 672, 261) | ~450 |
| `engineering_dispatch_admission.py` (899) | ~45% — `pr-conflict-` частные случаи (193-333, 502-547, 848-873) | `pr_conflict_dispatch.py`; парковки 464-499, 568-625 → `infrastructure_park.py` | 0 | ~400 |
| `routers/applications.py` (831) | CRUD 61-310; админ-действия 312-831 | `_application_admin_actions.py`; `_make_deploy_run_id` (335) и `_parse_github_repo_url` (401) → `src/deploy_runs.py`, `src/github_urls.py` (их импортируют другие роутеры) | 0 | ~310–520 |

**Предусловие для всех разрезов API — A6:** вынести `_story_helpers.py`, `_task_helpers.py` (кроме `to_read`), `projects_guards.load_locked_project`, evidence-функции `_pr_conflict_attempt.py:45-233` в `src/domain/`; admission-модули начнут импортировать сверху, цикл через `routers/__init__.py` исчезнет. Сохранить порядок блокировок (intent → story → run; «lock ladder» `engineering_dispatch_admission.py:103-109`).

### 5.8 `shared/` — крупнейшие модули

| Файл | Разрез | LOC | Риск |
|---|---|---|---|
| `live_harness_cleanup.py` (1 225) | пакет `shared/live_harness/`: `github_probes.py`, `registry.py`, `remote.py`, `targets.py`, `__main__.py` + перенести `live_contour.py`, `stand_deadlines.py`, `live_harness_workspaces.py`; хелпер `_github_get` вместо 6 ручных httpx-вызовов compare (211-220, 250-259, 303-316, 381-405, 468-477, 530-541) | ~−225 | путь модуля — CLI-контракт: 12 вызовов `docker exec … python -m shared.live_harness_cleanup` и 5 тестовых утверждений меняются одним PR; `REMOTE_DIAGNOSTICS_SCRIPT` ищется через `Path(__file__)` |
| `clients/github/_actions.py` (951) | `_repo_settings.py` 75-150; `_workflow_runs.py` 152-333, 669-683, 778-810; `_workflow_wait.py` 335-667, 812-951; `_workflow_logs.py` 18-69, 685-776; `GitHubAppClientBase._headers(token)` вместо 23 словарей | ~−170 | низкий; композиция в `github/__init__.py` сохраняется |
| `redis/client.py` (671) | `redis/validation.py` (89-153), `redis/dlq.py` (74-86, 494-566); протокол `StreamCodec` вместо 9 ветвлений `is_po_stream` | ~−220 из клиента | **высокий** — граница защиты секретов; только после характеризационных тестов |
| `contracts/dto/run_result.py` (649) | `run_result_engineering.py` (32-120), `run_result_deploy.py` (123-284), `run_result_qa.py` (287-646) | 0 | ~40 импорт-сайтов, codemod (без реэкспортов) |
| `contracts/dto/product_brief.py` (667) | `product_brief_limits.py` (52-113), остаются формы (116-459), `product_brief_admission.py` (462-667) | 0 | низкий |

**Перенос из shared (ФАКТ по импорт-графу):** модули с единственным производственным потребителем — `shared/models/*` (1 583, только api после A11), `qa_probe_library/` (298), `telegram_bot_probe.py` (259), `telegram_access_probe.py` (172) — только langgraph; `clients/infra_client.py` (47) — только scheduler; `constants.Paths/Provisioning` — только infra-service. Перенос ORM + QA-проб: −2.6 k строк (~25–30 k ток.) из контекста, который грузит не-API и не-QA работа (ОЦЕНКА). `worker_compose.py` и live-harness остаются — они исполняются в контейнерах через `docker exec`.

### 5.9 Документация как «гигантский файл»

| Документ | До, ток. | Предложение | После, ток. |
|---|---:|---|---:|
| `docs/CONTRACTS.md` | **DONE #687:** было 2 679 строк / ~47.4 k ток. обязательных | индекс + 8 `docs/contracts/*`; шаг TDD №1 теперь «индекс + релевантная граница» | индекс ~7 k ток.; типовая задача ~8–14 k с одним guide |
| `docs/CHANGELOG.md` (**477 после #686**) | **DONE #687:** ~9.4 k, но больше не default reading | Navigation = append/search when needed; не читать end-to-end по умолчанию | 0 по умолчанию |
| `docs/SECRETS.md` | **DONE #687:** было 991 строк / ~16.2 k ток. | production PO Redis/checkpoint procedures вынесены в `docs/runbooks/po-redis-and-checkpoints.md`; архитектурный документ сокращён | ~4–5 k; A21 остаётся из-за executable snippets в runbook |
| `docs/TESTING.md` | 13.6 k | удалить карточные нарративы 3-43, 57-89 | ~12.2 k |
| `docs/resource-management.md`, `VISION.md`, `docs/playbooks/line2-engineering.md` | 7.9 k достижимых | слить актуальное с SECRETS.md; архивировать остальное (`playbooks` сам объявляет себя устаревшим, строки 14-17) | 0 достижимых |

Итог после #687 (ОЦЕНКА): CHANGELOG больше не входит в default reading, а обязательный CONTRACTS-вход снижен с ~47 k ток. до индекс + релевантный guide (~8–14 k на типовую контрактную задачу). Нельзя руками править/перемещать `docs/PLATFORM_CAPABILITIES.md` — он генерируется `scripts/platform_capabilities.py:25-29` и проверяется `services/langgraph/tests/unit/test_platform_capabilities.py`.

### 5.10 Скрипты и тесты

- `scripts/check-ci-gate.py` (1 968): пакет `scripts/ci_gate/` — `workflow_shape.py` (~650), `test_coverage.py` (~280), `image_pins.py` (~270), `actions_infra.py` (~350), `budgets.py` (~300); `check-ci-gate.py` — тонкий `main()`. Контекст: 1 968 → ~300. Риск: тесты грузят файл по пути и патчат `gate.ROOT` (`scripts/tests/test_check_ci_gate.py:8-15`, `tests/unit/test_template_pin_single_source.py:26,181`) — `ROOT` должен остаться единым атрибутом, читаемым в момент вызова.
- `scripts/clean_live_tests.py` (1 314): удалить собственный HTTP/SSH-слой 703-817 в пользу `shared/live_harness_cleanup` (908-996); строковые GitHub-скрипты 485-578 → `cleanup_github_repo`; инвентарь 875-1097 → `scripts/live_inventory.py`; 5 копий psql-argv → `_psql(...)`. ~1 314 → ~900 + ~230.
- `scripts/stand_run.py` (962), `stand_acceptance.py` (1 002): разрезы по 3 файла ~300 строк (выигрыш умеренный).
- `tests/live/*` (~48 k строк) — следующая цель по контексту; в этом аудите содержательно не разбиралась (ограничение).

---

## 6. Доказанное дублирование и возможности сокращения

Каждый пункт: обе стороны с file:line, почему извлечение семантически безопасно, либо почему дублирование намеренное.

### 6.1 Каркас периодических циклов scheduler (ФАКТ, −370 строк)

Одинаковый шаблон в `scaffold_loop.py:21-44`, `story_completion_loop.py:21-39`, `qa_routing_loop.py:21-39`, `temporary_access_loop.py:21-39`, `owner_notification_loop.py:21-43`, `worker_reconciliation.py:76-95`, `lifecycle_supervision_loop.py:73-93`, `story_supervision_loop.py:30-61`, `pr_ci_loop.py:21-54`, `task_dispatcher.py:481-500`:

```python
redis_client = RedisStreamClient()
await redis_client.connect()
logger.info("X_started", interval=_X_interval())
try:
    while True:
        try:
            counts = await sweep(api_client, redis_client)
            logger.info("X_cycle", **counts)
        except Exception:
            logger.exception("X_cycle_error")
        await asyncio.sleep(_X_interval())
finally:
    await redis_client.close()
    logger.info("X_stopped")
```

Плюс «изолированные подметания» реализованы трижды (`lifecycle_supervision_loop._run_sweep` 38-49, `worker_reconciliation._run_reconciler` 37-48, inline в `story_supervision_loop.py:45-56`, `pr_ci_loop.py:37-45`).
**Предложение:** `runtime.periodic_loop(name, sweeps, *, cycle_event, error_event)` (~50 строк). **Безопасность:** имена событий передаются параметрами и остаются идентичными; `git grep` не нашёл потребителей имён вне тестов и CHANGELOG. Нюанс: `pr_ci` логирует `prs_merged`/`ci_failures_routed` из int-результатов — адаптер должен сохранить ключи.

### 6.2 API-клиенты сервисов

**6.2.1 Идентичные методы (ФАКТ):**

| Эндпоинт | scheduler `clients/api.py` | langgraph `clients/api.py` | Отличие |
|---|---|---|---|
| `POST stories/{id}/{fail\|human-review}` | `stop_story` 642-653 | `stop_story` 429-440 | идентичны; scaffolder `fail_story` `api.py:67-73` — третья форма |
| `GET repositories/` → primary | 128-134 | 520-526 | идентичны; оба сравнивают с литералом `"primary"` вместо `RepositoryRole.PRIMARY` |
| `GET stories/{id}` | 539-541 | 400-402 | идентичны |
| `GET tasks/?story_id=` | 676-678 | 409-411 | идентичны (+ scaffolder `api.py:64`) |
| `GET tasks/{id}/events` | 760-762 | 417-419 | идентичны |
| `GET product-briefs/by-story/{id}` | 522-535 | 292-304 | идентичны (404 → None) |
| `GET projects/{pid}/users/grant-intents/{iid}` | 227-230 | 165-167 | идентичны |
| `GET runs/{id}`, `list_active_incidents`, `get_task`, `create_task` | 263-265, 812-815, 700-702, 692-694 | 154-156, 271-274, 413-415, 421-423 | идентичны |
| `POST stories/{id}/{action}` | `transition_story` 624-640, тело `{"actor":"architect", qa_run_id?}` | 425-427, **без тела** | **расходятся** — оставить раздельными |
| `GET projects/{id}` | 90-97 | 458-469 (+`X-Telegram-ID`) | намеренно разные |

**Предложение:** read-mixin в `shared/clients/` (например `PipelineReadsMixin`) поверх уже общего `InternalAPIClient`. **Безопасность:** оба сервиса — внутренние вызывающие одного API, транспорт/заголовки уже общие; граница сервисов не пересекается. `transition_story` и методы с аудит-идентичностью остаются per-service. −60 строк на сервис (ОЦЕНКА).

**6.2.2 `PATCH runs/{id}` (ФАКТ):** 28–29 сырых `api_client.patch(f"runs/{...}", json={"status": RunStatus.X.value, ...})` в langgraph (`deploy.py:188,354,445,590,635,692,745,851`; `engineering.py:162,300`; `engineering_result_handler.py:311,348,356,391,650`; `qa.py:179,542,1103`; `_qa_grant_sweep.py:42,72`; `worker_spawner.py:586,688,744`). У scheduler уже есть `update_run` и `record_run_outcome_unless_settled` (`scheduler/src/clients/api.py:334,338`) — тот же шаблон «409 = уже урегулировано», что и `qa._update_run` (1102-1120) и `_qa_grant_sweep._report_residual_access`.
**Предложение:** перенести `RunUpdate` (`services/api/src/schemas/run.py:53`) в `shared/contracts/dto/run.py`; `update_run(run_id, RunUpdate)` и `settle_run_unless_settled(...)` — в общий mixin. −60…−80 строк; попутно ловит лишние ключи (например `"status":"queued"` в `engineering_result_handler.py:708-722`, молча отбрасываемый `RunCreate`).

**6.2.3 Deploy-обработчики сбоев (ФАКТ):** `deploy_failure_handler.py:25-79`, `deploy_result_handler.py:44-108`, `470-513`, `516-578` — одинаково: `DeployRunResult` → PATCH FAILED → callback `"failed"` → `live_work_unsettled({...})`; три inline-варианта без callback в `deploy.py:188-202, 590-600, 635-645`. **Предложение:** `record_deploy_failure(msg, redis, run_result, error_msg, *, extra=None)`. **Безопасность:** вызывающие по-прежнему строят свой типизированный результат; возвращаемый словарь остаётся `unsettled`, фенсинг teardown не меняется. −90 строк.

### 6.3 Создание и публикация deploy-run (ФАКТ, 4 копии, уже разошлись)

`scheduler/src/tasks/supervisor/deploy.py:1041-1072` (`deploy-retry-…`), `1437-1470` (`deploy-infra-…`), `1713-1743` (`deploy-secret-…`), `pr_poller.py:1025-1050` (`deploy-poll-…`): случайный id, `create_run({...})`, поиск получателя, `DeployMessage(...)`, публикация. Расхождения: `action="feature"` (`deploy.py:1068, 1739`) vs `DeployAction.FEATURE` (`:1465`); `pr_poller` шлёт `"type":"deploy"` и без `status` (`1026-1036`).
**Предложение:** `supervisor/deploy_dispatch.publish_deploy(...)`, порядок create → publish сохраняется дословно (исправление порядка — отдельная задача, §9.11). −70 строк.

### 6.4 «Провалить story, затем оповестить админов» (ФАКТ)

19 вызовов `api_client.fail_story(` в `scheduler/src/tasks/`, из них 17 сразу сопровождаются `_notify_admin_failure` (например `supervisor/deploy.py:531-534, 543-549, 571-574, …, 1717-1720`; `supervisor/common.py:105-106`). Хелпер уже есть, но используется однажды: `_fail_deploy_fix_handoff` (`deploy.py:826-831`). Обобщить в `common._fail_and_alert(...)`, сохранив различающийся `entity_id` (run.id или story_id). −35 строк.

### 6.5 Инфраструктура воркер-каналов (ФАКТ)

| ID | Сторона A | Сторона B | Вывод |
|---|---|---|---|
| W1 | broker `auth.py:8-16` (`credential_key`, `token_digest`, `verify_token`) | `worker-manager/src/manager.py:133-136`, `routers/compose.py:49-53` | вынести в `shared/contracts/worker_control_plane.py`; HSET в `manager.py:133-136` избыточен — `register_worker` брокера (`main.py:145-155`) пишет тот же дайджест в тот же ключ |
| W2 | `manager.py:1249-1254` запрещённые env | wrapper `config.py:58-66` и `88-93` | одна константа `FORBIDDEN_WORKER_TRANSPORT_ENV` (wrapper может её импортировать) |
| W3 | `worker-manager/src/agents/*.py` (~93 строки; используется только `get_instruction_path`, `manager.py:1276`) | `injected_paths.py:60-68` `instruction_filename()` («mirrors» агентов) | `WorkerWorkspace.instruction_file(agent_type)` в `shared/constants`, удалить пакет `agents/` (−100) |
| W4 | создание группы + BUSYGROUP: `shared/queues.py:96-123`, broker `main.py:156-162`, spawner `696-701`, `841-845`, `1060-1064`, `qa_worker.py:188-192` | — | `shared.redis.ensure_group(...)` (broker уже включает `shared/redis`) |
| W5 | `DeleteWorkerCommand` XADD: spawner `292-297`, `962-967`, `qa_worker.py:290-300` | готовый `publish_worker_deletion` (`worker_spawner.py:766-773`) | вызывать готовый хелпер; префикс/причина — параметры |
| W6 | base64-в-exec: `manager.py:1279-1284, 1301-1306, 1343-1349` | chunked-писатель `_WRITE_LIBRARY_FILE` (`manager.py:60-64, 1368-1380`) | один писатель (−35), заодно снимается латентный лимит ARG_MAX (§9) |
| W7 | ключи `worker:status:{id}` (29×), `worker:meta:`/`workspace:lock:` (77×), `worker:{id}:input/output` (10× при наличии `WorkerChannels.INPUT_PATTERN`, `shared/contracts/queues/worker.py:57-58`), `po:response:{rid}` (7 написаний) | — | билдеры в `shared/queues.py` (`worker_status_key`, `worker_input_stream`, …) + codemod; строки идентичны |
| W8 | терминальные статусы воркера: `garbage_collector.py:30`, `manager.py:728` (без `GONE`) | `shared/contracts/dto/worker.py:22` `WORKER_TERMINAL_STATUSES` | `GONE` не персистится (только в ответах introspect `introspect.py:327,386`) — замена безопасна |

**Намеренное дублирование (оставить):** парсер `-f` в shim `compose_proxy.py` (исполняется как отдельный скрипт внутри воркера, не может импортировать `shared/worker_compose.py`); `exclude_worker_internal_files` в `runners/noop.py:307-341` (отдельный процесс, получает те же строки как данные).

### 6.6 Подтверждённый мёртвый код (ФАКТ по `git grep -w` по всему репозиторию)

| Где | Что | ~Строк |
|---|---|---:|
| langgraph `clients/api.py` | `list_projects`(101), `get_user_by_telegram`(127), `update_server`(131), `get_server_services`(134), `list_server_ports`(137), `allocate_server_port`(140), `update_deployment`(149), `create_incident`(254), `list_incidents`(276), `update_incident`(279), `get_story_owner_notification`(404), `get_task_events`(417), `get_allocation`(539), `release_allocation`(548), дубли `_get_json/_post_json/_patch_json` (86-96) | ~90 |
| langgraph | `worker_events.py` + `events.py` + задача `main.py:79` (канал `worker:events:all` без отправителя, поток `orchestrator:events` без читателя) | ~105 |
| langgraph `config/settings.py` | `default_agent_type` (29, **обязательный** — сервис не стартует без `DEFAULT_AGENT_TYPE`), `mount_claude_session` (39), `anthropic_base_url` (43), `qa_executor_agent_type` (74) | ~15 |
| langgraph `consumers/_base.py:323-325` | `msg.data.update(result)` — результат отбрасывается | 3 |
| scheduler `clients/api.py` | `admit_engineering_budget`(163), `release_engineering_budget_admission`(191), `create_run_if_absent`(250), `supersede_deploy_dispatch`(362), `get_live_temporary_access_grant_for_run`(438), `get_application_if_missing_returns_none`(851), `get_applications_by_project`(891) | ~90 |
| worker-manager | `pause_worker/resume_worker` (`manager.py:538-552`), `DockerClientWrapper.pause/unpause_container` (`docker_ops.py:78-86`), `_TERMINAL_STATUSES` (728), `_capture_removal_evidence` (`worker_removal.py:78-143`), `resolve_compose_path` (`compose_validator.py:865-878`), status-команда/ответ/обработчик (`consumer.py:181-190`; `StatusWorkerCommand` не производится) | ~200 |
| worker-wrapper | `_collect_and_archive` (`wrapper.py:1520-1523`, 7 тестов патчат невызываемый метод), `getattr(self.broker, "exhausted", False)` (285) | ~10 |
| infra-service | ключи результата `messages`/`current_agent`/`services_failed`/`incident_journal_status` (11+ мест, читатели отсутствуют, `main.py:228-274`), `nodes/__init__.py` (RetryPolicy/FunctionalNode), `InfrastructureAPIClient.create/list/update_incident` + `incidents.resolve_active_incidents`, запись `deploy:result:{id}` (`main.py:108-109,152`, читателей нет), константы `ansible_runner.py:38-39` | ~180 |
| scaffolder | `clients/api.py:36 get_repository`; `consumer.py:448` `msg.data.update(result)` | ~10 |
| telegram_bot | пустой `src/session/`, `ProactiveListener.stop/_running`, недостижимый `main.py:258` | ~10 |
| shared | `contracts/dto/service_deployment.py` (целиком), `contracts/events.py` (`ProgressEvent`), `contracts/queues/developer_worker.py`, `AnalyticsHourly/Daily/KnownUserDTO`, `ServerMetricsHistoryDTO`, `EngineeringResult`, `ensure_all_groups` (`queues.py:87-123`), `WorkerChannels.COMMANDS`, `GITHUB_ORG` (`live_harness_cleanup.py:33`), неиспользуемые ветки `required=False` в `config.py:74-107`, fallback `redis = None` (`redis/client.py:30-33`) | ~250 |
| scripts/infra | `scripts/agent_configs.yaml` = `[]` + `seed_agent_configs.py` (123) и 4 вызова (Makefile:567-569, `stand-e2e.yml:748-750`, `danger_prod_reset.py:466-467`, `api/Dockerfile:47-48`); `tests/fixtures/mock_github.py`; `tests/live/test_bot_access_revocation.py` (7 строк docstring, 0 тестов); `WORKER_BASE_IMAGE`/`ImageBuilder.base_image` (хранится, не читается, `image_builder.py:135`); `pytest-cov` в 5 Dockerfile.test без `--cov`; неиспользуемые маркеры `unit`/`integration`/`requires_github`; `--profile build` без профилей | ~250 |
| api | `PUT /api/projects/{id}` — побайтная копия PATCH (`projects/lifecycle.py:231-265` vs `268-306`, включая событие `"project_patched"`), вызывается только структурным тестом (ГИПОТЕЗА: нет внешних клиентов); `runs.py:672` повторный `pop`; `runs.py:261` неиспользуемый `x_telegram_id` | ~40 |
| admin-frontend | `types/api.ts:151-162 TaskStatus` | 12 |

Итого ≈ 1.3–1.5 k строк. **Условие безопасности:** удалять вместе с тестами, которые только утверждают существование/`assert_not_called` (например `test_supervisor_run_routing.py:291,654,764`, `test_task_dispatcher.py:242,245`, `tests/unit/test_settings.py:20`), и правками CONTRACTS.md (Review Trigger для `shared/contracts`). Удаление Ansible-наследия (`playbooks/site.yml`, `bootstrap.yml`, роли `backup/caddy/common/security/services`, ~600 строк YAML; `site.yml:23` ссылается на несуществующую роль `secrets`) — только после подтверждения владельцем (ГИПОТЕЗА: может запускаться вручную).

### 6.7 Инфраструктура, compose, Makefile (ФАКТ)

- Тестовые compose: `db` идентичен в 4 файлах, `redis` — в 6 → `tests/compose/_infra.yml` + `extends`. Условие: научить `check-ci-gate.compose_image_references` (:1070), `external_image_count` (:1677) и `ci-infra.sh pull-images` понимать `extends` (или использовать `docker compose config`). −120.
- `docker-compose.yml`: 5 сервисов образа langgraph (48-240) и 3 scheduler (399-523) повторяют `image/build/env_file/networks/restart/healthcheck` → якоря `x-langgraph`, `x-scheduler` + отдельный якорь env (merge `<<` одноуровневый). −100. `PYTHONPATH: /app` дублирует `ENV` в Dockerfile; 6 строк `restart: unless-stopped` в prod-overlay дублируют базу.
- Makefile: `rebuild-worker-images` и `-hard` отличаются только `--no-cache` (132-179, −22); «kill worker containers» ×3 (92, 112, 501); `lock-deps` перечисляет 8 сервисов вручную.
- `pytest.ini` ×4 почти идентичны, повторяют дефолты pytest.
- Фикстура `redis_client` определена 8 раз с разной семантикой (в т.ч. `RedisStreamClient` поверх fakeredis под тем же именем) — фабрика в `shared/tests/`.

### 6.8 Прочие доказанные дубли

| Где | Обе стороны | Вывод |
|---|---|---|
| api, валидатор переходов статуса | `_story_helpers.py:69-89`, `_task_helpers.py:170-190`, `brainstorms.py:44-65` | `validate_enum_transition(..., conflict_status=422)`; brainstorms передаёт 409 (сейчас отличается кодом — выравнивание к 422 — решение владельца). −40 |
| api, «lock project or 404» | хелпер `projects_guards.py:53-59`; inline `projects/secrets.py:176-180, 215-219`, `telegram.py:51-55`, `teardown.py:64`, `qa_probes.py:111-115`, `verification_gaps.py:82-86` | тот же запрос/блокировка/текст 404. −25 |
| api, auth-параметры | `X-Telegram-ID` ×47, `Depends(_optional_bearer_scheme)` ×46, `check_project_access` ×17 | зависимость `actor_context()` возвращающая контекст (не актёра — порядок 404/403 зависит от `load_locked_project`). −120…−150 |
| api, поиск свободного порта | `servers.py:421-432` (с повтором на `IntegrityError`) vs `applications.py:776-785` (без) | `next_free_port(...)` из servers-версии; попутно устраняет гонку §9 |
| api, QA-преамбула | `projects/qa_probes.py:102-129` vs `verification_gaps.py:73-100` | `_load_settled_qa_run(...)` |
| api/scheduler/langgraph, терминальные наборы | `runs.py:59`, `_story_helpers.py:35`, `api/.../temporary_access.py:32`, `langgraph/_base.py:104,109`, `scheduler/.../temporary_access.py:31`, `terminal_worker_reconciliation.py:15`, `access.py:352-354,446-448,519-523` | экспорт `RUN_TERMINAL_STATUSES` из `shared/contracts/dto/run.py`; наборы идентичны |
| langgraph, редьюсер `_merge_errors` | `graph.py:18-26`, `subgraphs/devops/state.py:11-19`, `subgraphs/engineering.py:28-36` | чистая функция, тела идентичны → `src/state_reducers.py` |
| langgraph, QA remote-обёртки | `agents/qa/tools.py:124-132, 134-143, 145-153, 155-161` | `_bounded(tool, request, call, observe_subject=None)` (у `container_inspect` нет observe) |
| langgraph, детали ошибок API | `tools_stories.py:72-76`, `tools_briefs.py:409,545`, `tools_notices.py:72,74`, `tools_projects.py:231,249,269,290,313,350`, `architect/tools.py:192-205` | `api_detail(response)`; устраняет `JSONDecodeError` на не-JSON 4xx |
| langgraph, разбор GitHub URL | `deployer.py:483-488`, `env_contract_loader.py:22-28`, `secret_resolver.py:528-540`, `developer_tasks.py:17-18`, `consumers/deploy.py:238-241` (+ api `_parse_github_repo_url`) | **поведение различается** (strip `.git`, валидация) — единый `parse_github_repo` в shared меняет поведение на нестандартных URL; сначала характеризационные тесты |
| langgraph, дефолт `modules` | `developer.py:83` `["backend"]`, `resource_allocator.py:52` `["backend"]`, `deployer.py:166` `"backend"`, `secret_resolver.py:68` `[]`, `smoke.py:97` `[]`; `consumers/deploy.py:111`, `allocations.py:104-105` | три разных дефолта; единый аксессор fail-fast (ГИПОТЕЗА: `ProjectCreate` всегда задаёт `modules`, дефект латентный) |
| shared, enum | `shared/notifications.py:159-170 AdminDeliveryStatus` = `contracts/dto/executor_diagnostics.py:427-433 ExecutorProfileAlertOutcome` (мост `profile_alerts.py:216`) | перенести в `contracts/vocab.py`, удалить дубль; значения на проводе те же |
| shared, реестр | `live_harness_cleanup.py:589-596` = `clients/registry.py:88-99` | безопасно (`RegistryError` ⊂ `RuntimeError`) |
| api/telegram_bot, админы | `api/src/config.py:45-51`, `telegram_bot/src/config.py:40-48` `get_admin_ids` | общий хелпер в `shared.config` с валидацией (сейчас некорректные id молча отбрасываются `.isdigit()`) |
| telegram_bot, dashboard-токен | `main.py:112-122` vs `handlers.py:195-210` | тот же ключ `lk_token:{uuid}`, TTL 300; префикс также в `api/.../lk_auth.py:15` |
| infra-service | фейл-ветки `node.py:279-300, 334-349, 362-381, 425-439`, `handlers.py:36-77`; последовательность access→cutover→labels→software `node.py:319-423` vs `operations.py:491-559` | `_fail_step(...)` (−45), `run_access_then_software(...)` (−60, имена шагов инцидентов закреплены тестами) |
| scaffolder | ~12 копий «detail → result.error → log → return» в `scaffold.py:137-347, 396-498`; git identity 213-224 = 470-480; `consumer.py:250-257` = `_record_scaffold_error` 383-396 | хелперы; имена событий сохранить |
| scripts | SSH/ключ сервера `clean_live_tests.py:720-817` vs `shared/live_harness_cleanup.py:908-996`; GitHub-org константа ×3; 5 ручных `urllib`+`X-Internal-Key` в stand-скриптах | стандартная библиотека `scripts/_internal_api.py`; `org` в `Contour` |

### 6.9 Отвергнутые/намеренные дубли (не объединять)

- `StoryRead`/`TaskRead` (api) vs `StoryDTO`/`TaskDTO` (shared): поля совпадают, но API-схемы типизируют `status/type` как `str`, DTO — enum; переключение `response_model` превратит плохое значение БД в 500 и изменит OpenAPI. Держатся тестом паритета. Объединять по одной сущности после проверки её persisted values; #685 типизирует только Run vocabulary.
- PO-цикл (`po.py:177-237`) vs `_base.run_queue_worker`: разные требования (per-chat блокировки, семафор, DLQ, нет live-work lease) — слияние добавит ветвления в `_base`.
- Двойная проверка идентичности Time4VPS (`node.py:178-193`, `operations.py:434-447`) — намеренный TOCTOU re-check (комментарий `node.py:550`).
- Общий пакет для двух фронтендов (`ApiError`, `request<T>`, `cn()` ~20 строк) — выгода меньше стоимости workspace-пакета.
- Повтор фразы-запрета в PO-промпте (6 раз, `prompts/po/__init__.py:223…281`) — закреплён тестами по секциям (`test_po_prompts.py:224,248,447,449`); промпт 15 946/16 000 символов. Требует продуктового решения.

---

## 7. Расхождения схем/API/событий/конфигурации

### 7.1 Вход рабочего потока без контракта (ФАКТ, P1)

- CONTRACTS.md:1077-1078, 1212, 1562 указывают источник `shared/contracts/queues/developer_worker.py`; `DeveloperWorkerInput/Output` импортируются только `services/langgraph/tests/conftest.py`.
- Реальные производители: `worker_spawner.py:714-729` (`request_id, attempt_id, turn_deadline_seconds, prompt, user_id: 0, story_md?, branch?`), `1066-1083` (+`clear_session`, без `user_id`), `qa_worker.py:253-255` (`request_id, prompt, user_id: 0`, без `attempt_id` — брокер трактует как «без надзора», `worker-broker/src/main.py:68`).
- Потребители: broker `data.get("attempt_id")` (`main.py:61-89`), wrapper `data.get("prompt")`, `data.get("content")` (`wrapper.py:723-731, 1532`), `task_id` (345, 1587) — поля, которые никто не шлёт; `user_id: 0` никто не читает (и это именно то поле, которое `contracts/recipient.py` существует, чтобы отвергать).
- **Предложение:** `WorkerTurnInput` в `shared/contracts/worker_turn.py` (опциональные `attempt_id/turn_deadline_seconds` сохраняют семантику QA), использовать у обоих производителей, брокера и wrapper; удалить `developer_worker.py`. Изменение `shared/contracts` → Review Trigger (AGENTS.md).

### 7.2 **DONE #682** — Обработка «ядовитых» сообщений

PR #682 унифицировал failure semantics поверх существующего `RedisStreamClient`: публичный
`reject_entry()` сначала копирует terminal payload в `{stream}:dlq` и только после успешного
quarantine ACK-ает исходную запись; `reject_if_exhausted()` использует durable PEL delivery count
с default ceiling 5. LangGraph, scaffolder, infra-service, scheduler provisioner-results и Telegram
consumers используют этот путь для malformed/permanent work; transient failures остаются pending
до bounded reclaim. Provisioner notifier больше не `auto_ack=True`, а делает manual ACK после
обработки и reclaim pending work. Scheduler 429/5xx больше не превращает в успешный ACK.
`RedisStreamClient._iter_entries` пробрасывает `CancelledError`, поэтому shutdown/timeout не
маскируется как нормальное завершение и не тратит delivery budget.

PO proactive сохраняет свой более узкий delivery ceiling и admin alert, но invalid/exhausted записи
теперь тоже карантинируются. Валидный happy path не менялся; полный CI, включая LangGraph service
tests и Required CI Gate, прошёл. Чисто механический `StreamCodec` split из §5.8 остаётся открытым
и перенесён в PR 12, где не смешивается с correctness semantics.

### 7.3 Типизация HTTP-тел и статусов (ФАКТ)

- **DONE #685 — Run type/status:** `RunBase` использует `RunType`/`RunStatus`, shared `RunCreate.type` и фильтры списков типизированы; неизвестные значения и явный `RunUpdate.status: null` дают 422. Пропуск `status` не меняет его. БД сохраняет `String(50)` с `ck_runs_type_valid`/`ck_runs_status_valid`; неизвестные persisted values останавливают миграцию до изменений. Paid creation gate, terminal ownership и существующая nullability `project_id/result` сохранены. Унификация `RunRead.result: dict` с shared result DTO в эту итерацию не входила.
- `RunDTO.project_id: str` обязателен (`dto/run.py:77`), колонка nullable (`models/run.py:24`), API отдаёт `UUID | None`; langgraph разбирает `RunDTO.model_validate` (`clients/api.py:156,163,206`).
- `TemporaryAccessGrantDTO.channel/external_id: str` обязательны, колонки nullable (`dto/temporary_access.py:97-98` vs `models/temporary_access_grant.py:34-35`).
- `ProjectDTO.description/modules` (`dto/project.py:131,133`) — колонок нет, всегда `None/[]`; scheduler читает `project.description` (`scaffold_trigger.py:215`) — фактически мёртво.
- Ad-hoc ответы без `response_model`, пересекающие границы сервисов: `spawn-worker` (`_task_actions.py:516`, две формы 578/655; фронтенд типизирует вручную `SpawnWorkerResponse`), `run-e2e` (`applications.py:585`), `complete_intent` (`projects/access.py:1169`, тело `GrantIntentCompletion` определено локально на :72), `GET /servers/{handle}/incidents` (`servers.py:833-865`, без `server_handle`, который ожидает фронтенд).
- Статусы литералами вместо shared enum (значения сейчас верны, но не принуждены): scheduler `run_type="deploy"/"qa"` (`supervisor/deploy.py:270,1617`, `qa.py:103`), `"detected"/"resolved"` (`clients/api.py:794,806`), `"human-review"` ×5 при наличии `STORY_HUMAN_REVIEW_ACTION` (`supervisor/common.py:25`); langgraph `deployment_result.status` `"cancelled"` vs `DeploymentResult` с `"canceled"` (одна «l») и без `"error"` (`shared/contracts/dto/deployment.py:6-12`) — **нужно решение по написанию**; PO-инструменты сравнивают статусы story строками (`tools_stories.py:268,331,365,394,433,464,488`); api `type="deploy"` (`applications.py:384,480,559,797`), `Repository.role == "primary"` (3 места).
- Аудит-идентичность: scheduler `transition_story` всегда шлёт `{"actor":"architect"}` (`scheduler/src/clients/api.py:636`) для pr_review/deploy/test/complete/human-review; `fail_story` шлёт `"supervisor"` (:553), `retry_story_after_ci_failure` — `"scheduler"` (:581).
- `DeployMessage.task_id`/`EngineeringMessage.task_id` несут **id Run** (`_task_actions.py:614`, `consumers/deploy.py:976`), а `planning_task_id` — id Task; staleness guard (`_base.py:137-140`) на этом основан. Путаница имён — кандидат на переименование в `run_id` (изменение контракта).

### 7.4 **DONE #685** — Персистентность: модели vs миграции

Исходные 14 различий воспроизведены в RED CI первой ревизии #685; после исправления
`compare_metadata` на полной цепочке миграций возвращает пустой список. Закрытие каждого пункта:

| # | Исходное различие | Решение #685 | Проверка / ограничение |
|---|---|---|---|
| P1 | 5 лишних колонок `temporary_access_grants`: `observation_id`, `slot_clear_since`, `slot_clear_readings`, `observed_at`, `reopened_at` | явное удаление миграцией `b2d4f6a8c0e1`; подтверждено прежнее удаление их lifecycle из кода | любой non-default value останавливает upgrade до DDL/data changes; downgrade возвращает пустые поля |
| P2 | partial unique indexes `uq_incidents_active_provisioning_failure` и `uq_incidents_active_target_not_ready` отсутствовали в ORM | точные определения добавлены в `shared/models/incident.py`; существующие индексы не пересоздаются | duplicate-active/cross-type/server/resolved cases проверяются и на ORM-схеме, и на клоне мигрированной таблицы; другие incident families не ограничиваются |
| P3 | четыре nullable timestamp колонки и различное представление уникальности `product_briefs.story_id` | известные даты сохранены; отсутствующая берётся из второй даты либо migration time; NOT NULL; отдельные UNIQUE constraint и nonunique index в ORM | PostgreSQL upgrade→downgrade→upgrade и все комбинации отсутствующих дат; metadata comparison без ignore-фильтров |
| P4 | `runs.type/status` — VARCHAR без CHECK | `ck_runs_type_valid`/`ck_runs_status_valid`, VARCHAR сохранён | INSERT/UPDATE вне vocabulary отвергаются; все текущие RunType × RunStatus проверяются на мигрированной БД; статусы прочих моделей не включены в закрытие |

**Постоянный gate:** `services/api/tests/service/test_schema_metadata.py` выполняет Alembic
`compare_metadata` внутри существующего PostgreSQL service suite после `alembic upgrade head`.
Поведенческие проверки дополняют сравнение: Alembic сам не сравнивает CHECK и WHERE partial index.
Отдельного CI job или общей фильтрации drift не добавлено. Миграция берёт ACCESS EXCLUSIVE locks
на четыре затронутые таблицы; данные production не исследовались, поэтому несовместимые строки
явно блокируют upgrade с образцами ID вместо автоматического исправления неизвестных значений.

### 7.5 Реестр очередей в CONTRACTS.md vs код (ФАКТ; 31 утверждение проверено, 13 не совпадают)

Неполные производители: `architect:queue` (1071; собственная таблица 1557 противоречит), `engineering:queue` (1072, 1558 — нет API), `deploy:queue` (1073 — нет langgraph), `worker:commands` (1075 — нет scheduler и worker-manager), `provisioner:queue` (1079, 1563 — назван scheduler-infrastructure; реально API `servers.py:885` и langgraph `provisioner_client.py:44`). Несуществующее: `task_progress:{task_id}` из `events.py` (1084, 1565; GLOSSARY:219). Мёртвый тип: `EngineeringResult` (1558). `COMPOSITE_CHAINS` содержит 2 записи, а не одну (1374 vs `_story_actions.py:377-387`). `routers/projects.py` → теперь пакет (1342, 1538). Все 61 процитированных пути `/api/...` существуют.

### 7.6 Конфигурация (ФАКТ)

- Один параметр — разные имена: `BROKER_INTERNAL_TOKEN`/`SESSION_TTL_SECONDS`/`STREAM_MAXLEN` (broker `config.py:7-9`) vs `WORKER_BROKER_*` (manager `config.py:23-25`), переотображение в `docker-compose.yml:318-319,377-380`; Time4VPS: `TIME4VPS_USERNAME` (`shared/clients/time4vps.py:78`) vs `TIME4VPS_LOGIN` (`deploy.yml:330`, `.env.example:82`), infra-service читает оба (`node.py:166`), scheduler — из API-хранилища ключей (`server_sync.py:114`); workspace: `SCAFFOLDED_WORKSPACE_PATH` vs `WORKSPACE_BASE_PATH` (оба `/data/workspaces`, `docker-compose.yml:330,538`).
- Двойные дефолты политики: `QA_EXECUTOR_AGENT_TYPE` (compose `:-codex` + `api/src/config.py:41` `AgentType.CODEX` + `langgraph settings.py:74`), `WORKER_BROKER_SESSION_TTL_SECONDS`, `QA_CAPABILITY_HOST`.
- `IMAGE_PUBLICATION_TIMEOUT_SECONDS = 900` дважды (`scheduler/tasks/image_publication.py:42`, `langgraph/subgraphs/devops/image_gate.py:36`).
- Нарушения правила env из AGENTS.md: connectivity-дефолты в worker-manager/broker (A19); отсутствие `GITHUB_APP_ID` лишь логируется, JWT подписывается с `iss=None` (`shared/clients/github/_base.py:31,41-42`); fallback `GITHUB_PRIVATE_KEY_CONTENT` (`_base.py:140-146`); 11 параметров политики с дефолтами в `shared/constants.py:28-62` и `NOTIFICATION_RATE_LIMIT` (`shared/notifications.py:46`).
- `pyproject.toml:44-54` (uv workspace) и smoke `compileall` в `workspace.toml` не включают `services/worker-broker`.
- Compose без top-level `name:` при захардкоженном `codegen_orchestrator` в `Makefile:506`, `danger_prod_reset.py:62-85`, `infra/promtail.yml` — в worktree с другим именем `make nuke` молча оставит том БД (ФАКТ по механике).

### 7.7 Фронтенд ↔ API (ФАКТ)

Гейт (`test_admin_frontend_contract_gate.py`) покрывает Dashboard/Users/Projects/Tasks/Settings — там дрейфа нет. Вне гейта: `ApplicationHealthMetrics.ssl_days_remaining` (`types/api.ts:545`, отрисовка `ApplicationDetailPage.tsx:329`) — производитель пишет `ssl_expires_at` (`scheduler/tasks/app_health_prober.py:168-175`), колонка всегда «-»; `QACheck {name, pass, detail}` (`types/api.ts:558-562`) vs `QAFailedCheck {name, detail, cause}` (`run_result.py:306-317`); `Incident.server_handle` не возвращается (§7.3); `MetricsSnapshot` поля должны быть `number | null`. user-dashboard `types/api.ts` совпадает с `schemas/lk.py` полностью.

---

## 8. Документация vs реализация

### 8.1 Высокая важность (вводит агента в заблуждение; ФАКТ)

| # | Документ:строка | Утверждение | Реальность |
|---|---|---|---|
| D1 | `ARCHITECTURE.md:135, 182` | scaffolder ставит `status=scaffolded` | такого `ProjectStatus` нет (`shared/contracts/dto/project.py:18-21`); ставится `ACTIVE` (`scaffolder/src/consumer.py:244`); та же страница говорит `active` на :34 |
| D2 | `ARCHITECTURE.md:75`, `README.md:59`, `GLOSSARY.md:15,20` | `langgraph` — engineering/devops подграфы; «нет engineering-consumer сервиса» | `langgraph` = только PO (`main.py:6-11`); подграфы — в `engineering-worker`/`deploy-worker`; compose определяет `engineering-worker`, `deploy-worker`, `qa-worker`, `architect` |
| D3 | `ARCHITECTURE.md:212` | DevOps: «Ansible deployment via infra-service» | deploy через GitHub Actions `deploy.yml` (`deployer.py:1`, `deploy_workflow.py:18`); infra-service запускает `deploy_project.yml` только при восстановлении после инцидента (`recovery.py:52`) |
| D4 | `ARCHITECTURE.md:214` | каждый потребитель — `consume()/consume_typed()` с `claim_pending=True`, poison → `{stream}:dlq` | см. §7.2 |
| D5 | `CONTRACTS.md:1077-1078, 1084` | источник worker I/O — `developer_worker.py`; поток `task_progress` | мёртвые DTO; поток не существует |

### 8.2 Средняя важность (устаревшее/противоречивое)

- `ARCHITECTURE.md:27-28` — списки статусов без `waiting_user_secret`, `archived` (story) и `waiting_resources` (task); :127, :191 — ребро `PO → deploy:queue` (PO не импортирует `DEPLOY_QUEUE`); :160 — `Dispatcher -.-> po:proactive` (scheduler пишет только в `po:input`); :198 — PR создаёт «Dispatcher» (реально цикл story_completion, `story_completion.py:58`; строка 39 той же страницы верна); :213 — QA-потребитель «создаёт fix task» (создаёт supervisor scheduler; PIPELINE_V2.md:537 верен); :12, :209 — нет Codex.
- `README.md:34-36` — диаграмма противоречит ARCHITECTURE по публикации в `deploy:queue`/`qa:queue`; :53-70 — нет `worker-broker`, `caddy`, `registry`; :89 — `make test-integration` «требует поднятый стек» (ложно: интеграционные compose автономны); :112 — Ansible в `infra/` (он в `services/infra-service/ansible/`); :125 — «три уровня секретов» vs SECRETS.md:7 «два».
- `docs/NODES.md:15-48` — список PO-инструментов в `tools.py`; инструменты разнесены по 5 модулям, 11–12 из 29 не документированы (`grant_project_user`, `get_initial_owner_deployment`, `retry_initial_owner_deployment`, `transfer_project_ownership`, `present_product_brief`, `confirm_product_brief`, `show_full_brief`, `suppress_owner_notice`, `resolve_deferred_notice`, `record_unverified_decision`, `get_story_diagnostics`, `pass_capability_request`); :182, :320 — `trigger_deploy` не существует; :150 — `_wait_for_ci_and_fix` не существует; :185-196 — дерево `devops/` (реально 10 файлов, `nodes.py` содержит только `ReadinessCheckNode`); :198-227 — топология начинается не с `resource_allocator` (`devops/graph.py:101,109-110`); :227 — deployer «ставит project status=active» (не ставит); :54 — PEL «на старте» (реально sweep каждые 30 с, `po.py:161-169,221-223`); нет разделов Architect/QA.
- `docs/LOGGING.md:159-259` — 20 имён событий не существуют в коде (`project_updated`, `spawning_developer_worker`, `health_check_start`, `message_sent`, …).
- `docs/SECRETS.md:11,22` — `POSTGRES_URL`, «K8s Secrets», `os.getenv` (реально `DATABASE_URL`, нет Kubernetes, pydantic settings); `SECRETS.md:37` и NODES.md — resolver «шифрует» (реально API шифрует на `POST /projects/{id}/config/secrets`, `api/routers/projects/secrets.py:143`).
- `docs/parallel-workers.md:51`, `GLOSSARY.md:28,33`, `resource-management.md:132,135,136` — эфемерные пространства `/tmp/codegen/...` для «standalone задач», удаление освобождает диск, GC каждые 30 мин (реально: эфемерные только у QA `{SCAFFOLDED_WORKSPACE_PATH}/qa-{id}`, developer-пространство сохраняется `worker_removal.py:376,454`, workspace GC — каждые 6 ч с порогом 35 ч `main.py:88-94`); `parallel-workers.md:75-80` — удалённые Prompts-эндпоинты.
- `docs/coding-agents.md:141-144` — auto-resume для всех агентов (только Claude, `wrapper.py:467-472`), compose-прокси «к worker-manager» (через broker), Makefile через `curl localhost:9090` (заменён shim `DOCKER_COMPOSE`), «агент коммитит и пушит» (авторитетный push делает wrapper, `wrapper.py:608-640`).
- `docs/resource-management.md:27,121,125,141` — `tools/allocator.py` (нет), `~/.gemini/keys/github_app.pem`, `/opt/secrets/github_app.pem`, мёртвая ссылка `tasks/secrets-vault-implementation.md`.
- `AGENTS.md:44` — шаблон пути тестов без слоя `service/`, который тот же AGENTS.md:77 рекомендует; :49 — `python3 -m secretary` недоступен в тулчейне репозитория (нет в `pyproject`/`uv.lock`) — нужна оговорка «иначе `make test-unit`»; :24 — машинно-локальный путь.
- `docs/VISION.md:92-99` — «инварианты» противоречат AGENTS.md (#4 запрет `.get(key, default)`; #5 «ничто иное не называется worker» при сервисах `*-worker`; #7 «shared только контракты»).
- Кодовые docstring: `stage_notices.py:29`, `liveness.py:206` (про «dispatcher tick»), `consumers/engineering.py:1`, `deploy.py:1` (`jobs:engineering`), `_base.py:3`, `langgraph/main.py:5`, `worker-manager/consumer.py:47-51, 117-123`, `garbage_collector.py:47-56`, `wrapper.py:342-343`, `http_server.py:5,170`, `scaffolder/validation.py:3-8`, `run_result.py:7-8` (`_check_result_matches_type` → `_check_result`), `architect/graph.py:4` («MemorySaver only» — компилируется без checkpointer).

### 8.3 Правила AGENTS.md vs практика (ФАКТ)

- `print()`: 210 вне тестов (184 в `scripts/` — CLI), реальные нарушения в долгоживущих процессах: `worker-manager/src/qa_egress_proxy.py:216,225,229,243,248`, `worker_wrapper/main.py:25`, `runners/noop.py:451`. Правило не принуждается: `ruff.toml:11-21` не выбирает `T20`, поэтому `# noqa: T201` ничего не подавляет. worker-manager и worker-broker вообще не вызывают `setup_logging`.
- Ad-hoc словари в очередях — §7.1; ключи стримов строятся inline — §6.5 W7.
- CHANGELOG: 4 слишком длинных записи в сентябре (строки 393, 451, 508, 693) и легаси-формат с 1542.


### 8.4 Executable docs как источник кода (ФАКТ; новый A21)

При #687 выяснилось, что часть production runbook не просто документируется, а фактически является
исполняемым источником: `test_po_maintenance_preflight.py` читает Markdown, вырезает Python-функции и
исполняет их через `exec(ast.parse(...))`; `test_backup_rootless.py` regex-ом извлекает fenced bash
blocks и валидирует их как shell. Это проверяет синтаксис/локальную механику snippet-а, но **не**
гарантирует, что prose вокруг него семантически соответствует текущей архитектуре. Зато любой перенос
документа создаёт скрытый coupling с Python-тестами.

**Предложение:** production/operator logic вынести в обычные `.py`/`.sh` entrypoints; unit tests
импортируют/запускают их как код, а runbook только ссылается на стабильную команду. Для Markdown оставить
дешёвые проверки ссылок/якорей/существования команд и, где полезно, коротких API examples. Не использовать
Markdown как канонический контейнер production predicate/script. Включить в PR 12 рядом с scripts/CI cleanup.

### 8.5 Проверено и корректно (ФАКТ)

Имена очередей и групп в CONTRACTS.md:1070-1083; `ANONYMOUS_ROUTES`, bearer-aware route classification и тест глобального гейта после #683; «десять циклов» scheduler-pipeline; маршруты `/api/...` в документах (все 61); все `make <target>` в документации существуют; все 51 `secrets.*` в `deploy.yml` описаны в DEPLOY.md; 30 внутренних якорей документов разрешаются.

---

## 9. ОЧЕВИДНЫЕ БАГИ / ОЧЕВИДНЫЕ НЕЭФФЕКТИВНОСТИ

> Отдельно от архитектурных рекомендаций. **Ничего не исправлялось.** Для каждого — доказательство и уровень уверенности.

### 9.1 **DONE #680** — Deploy после неудачной записи секретов
Закрыто PR #680: неполная запись обязательных GitHub Actions secrets теперь завершает deploy fail-closed до fence/dispatch.
`services/langgraph/src/subgraphs/devops/deployer.py:695-714`: при `secrets_ok == False` только `logger.error("deploy_secrets_write_failed")`, далее fence и dispatch. `GitHubAppClient.set_repository_secrets` глотает посекретные ошибки и возвращает счётчик (`shared/clients/github/_secrets.py:140-153`). Если не записался `DOTENV`, `deploy.yml` развернёт предыдущий `.env` (включая прежние ссылки на образы); `_verify_deployed_sha` проверяет только коммит, поэтому результат — «успех» с записью `image_references/image_digests` (792-817), которые не развёрнуты. Теста на `secrets_ok=False` нет.

### 9.2 **DONE #680** — Deploy-блокировка снимается не-владельцем
Закрыто PR #680: lock хранит уникальный lease-token и освобождается atomic compare-and-delete только текущим владельцем.
`consumers/deploy.py:437` `SET NX` → при неудаче `_claim_deploy_job` возвращает `DeployTerminal` (458-460), `process_deploy_job` выходит изнутри `try` (983-985), а `finally` (1032-1033) безусловно делает `DEL lock_key` — удаляется чужая блокировка; при успехе удаление тоже без проверки владельца (после истечения TTL удалит блокировку нового владельца). QA делает правильно — ранний выход до `try` (`qa.py:680-685`). Сейчас deploy-worker — 1 слот/1 реплика, пересечение возможно при рестарте/PEL-replay; повтор собственного `task_id` при этом отменит собственный run (445-457, ГИПОТЕЗА о реакции супервизора). Противоречит `docs/PIPELINE_V2.md:510`.

### 9.3 **DONE #681** — удаление входного потока воркера во время длинного хода
PR #681 закрыл гипотезу как реальный unsafe cleanup path: для `worker:*:input/output` idle threshold
больше не является достаточным основанием удаления. Scheduler извлекает worker id и сохраняет канал,
пока существует `worker:meta:{id}`; orphaned streams по-прежнему удаляются по idle threshold.
Регрессия покрывает час простоя живого worker-stream без `OBJECT IDLETIME`/DELETE.

### 9.4 **DONE #683** — LK-токен больше не открывает внутренние маршруты API
PR #683 разделил application-wide authentication и route authorization. Валидный `X-Internal-Key`
по-прежнему аутентифицирует внутренний сервис; валидный LK JWT после проверки подписи допускается
к handler только если соответствующий `APIRoute` имеет отдельную bearer-aware dependency вне
глобального гейта (`get_lk_user`, owner/current-user/admin guard через dependency tree). Маршрут без
такого opt-in теперь internal-only by default; это закрывает прежнюю поверхность вроде `GET /api/users`
и CRUD/operational routes, где одного валидного LK bearer раньше было достаточно. Анонимный allowlist
остаётся ровно `GET /`, `GET /health`, `POST /api/lk/auth/token`. Registry/regression tests фиксируют
классификацию representative routes и отдельно доказывают, что даже admin LK token не превращается
в сервисный credential.

### 9.5 **DONE #681** — Workspace GC project_id/repo_id
PR #681 разделил две области идентификаторов: `workspace:active_projects` остаётся project_id fence,
а каталоги scaffolded workspace защищаются по `repo_id` из live `worker:meta:*`. Metadata теперь
сканируется один раз за sweep вместо O(projects × workers); `.compose-plans` и `qa-*` исключены из
repository GC. Старые repo directories удаляются только при отсутствии live worker ownership.

### 9.6 **DONE #682** — infra-service бесконечно повторял некорректное сообщение
PR #682 отделил validation входного `ProvisionerMessage` от внутренних processing errors:
malformed payload quarantine-ится в DLQ перед ACK, а реальные processing failures остаются pending
до общего bounded delivery ceiling. Тем самым poison больше не возвращается каждые 60 с бесконечно.

### 9.7 Очистка очередей при удалении проекта ничего не удаляет — СРЕДНЯЯ (ФАКТ)
`services/api/src/routers/projects/teardown.py:294`: `fields.get("project_id")`, но все производители пишут `{"data": "<json>"}` (`shared/redis/client.py:212`) — совпадений нет. `QA_QUEUE` отсутствует в `_QUEUES_TO_CLEAN` (:274). (Если бы совпадало, XDEL записи в PEL дал бы ошибки `stream_entry_lost_to_trim`, `client.py:355-362`.)

### 9.8 Повтор упавшей задачи может застрять в BACKLOG — ВЫСОКАЯ (ФАКТ механизм; средне-высокая уверенность)
`scheduler/src/tasks/supervisor/liveness.py:486-488` — три отдельных вызова: `transition(BACKLOG)`, `transition(TODO)`, `update_task(current_iteration+1)`. При сбое второго задача остаётся в BACKLOG, который никто не обрабатывает (`:482` явно пропускает BACKLOG; супервизор смотрит FAILED `:333`, диспетчер — TODO `task_dispatcher.py:452`). При сбое третьего — повтор без увеличения итерации (ГИПОТЕЗА о достижимости лимита).

### 9.9 **DONE #685** — API и БД ограничивают Run type/status
Неизвестные значения и явный `status: null` отклоняются HTTP 422 до изменения строки; пропущенный
статус сохраняется. Run CHECK защищают прямую запись в БД. Реальные HTTP/service tests проверяют
отсутствие побочных изменений при отказе, допустимые значения, paid creation gate и фильтры списков.

### 9.10 **DONE #682** — Provisioner listener ACK-ал транзиентные ошибки API
PR #682 оставляет 429 и 5xx pending для reclaim вместо возврата из `_handle_failure` с последующим
ACK; после durable delivery ceiling повторяющийся сбой quarantine-ится в DLQ. Permanent/404
семантика остаётся terminal по существующему пути.

### 9.11 **DONE #679** — Story входила в DEPLOYING до создания deploy-run
Закрыто PR #679. Scheduler сначала сохраняет recoverable deploy Run с точным `DeployMessage`, затем выполняет требуемый Story transition и publish. PR-poller/retry/infra/user-secret используют стабильный id логической попытки; queued Run без dispatch stamp восстанавливается supervisor-ом вместо создания новой попытки.

### 9.12 Сбой публикации после коммита в админ-действиях API — СРЕДНЯЯ (ФАКТ)
`applications.py:485-496` (stop), `526-529` (undeploy), `562-578` (redeploy), `800-818` (from-repo), `stories.py:1166-1180` (send-to-architect): commit, затем `publish_message` без try/except → 500 и сущность в `STOPPING/DEPLOYING/IN_PROGRESS` с Run, который никто не обработает. Соседние маршруты обрабатывают это явно (503 + «outcome unknown»: `stories.py:994-1003`, `_task_actions.py:639-646`, `applications.py:678-685`).

### 9.13 Telegram-бот показывает все проекты как «Unknown» — СРЕДНЯЯ UX (ФАКТ)
`telegram_bot/src/keyboards.py:82`, `handlers.py:98`: `project.get("name", "Unknown")`; `ProjectRead` содержит `title`, не `name` (`api/src/schemas/project.py:29`).

### 9.14 Kill воркера в админке всегда выглядит как ошибка — СРЕДНЯЯ UX (ФАКТ)
`worker-manager/src/routers/introspect.py:454` — `204 No Content`; `admin-frontend/src/lib/api.ts:26` всегда `response.json()` → reject; `killMutation.onSuccess` (`WorkerDetailPage.tsx:36-40`) не срабатывает, хотя воркер удалён.

### 9.15 `/cancel` рекламируется, но не реализован — СРЕДНЯЯ UX (ФАКТ)
`telegram_bot/src/handlers.py:405,426` предлагают `/cancel`; `CommandHandler("cancel")` нет (`main.py:511-523`), команды отфильтрованы из `handle_message`. Пока установлен `awaiting_add_user`, любой нечисловой текст перехватывается (`main.py:345-347`) — админ не может общаться с PO. Введённый Telegram ID не отправляется в API (`handlers.py:431-440`), ответ 400 трактуется как «пользователь существует».

### 9.16 Прочие (ФАКТ, если не указано иное)

| # | Находка | Доказательство | Уверенность |
|---|---|---|---|
| a | Сбой одного элемента блокирует всё подметание каждый тик (нет поэлементной границы ошибок) | `supervise_deploying_stories` (`deploy.py:250-253`), `supervise_stuck_stories` (`liveness.py:127`), `supervise_waiting_resource_tasks` (:748), `poll_merged_prs` (`pr_poller.py:884`), `trigger_scaffolds` (`scaffold_trigger.py:76`), `complete_stories` | ФАКТ структура |
| b | **DONE #681** — compose timeout chain: manager phase ≤840 с, wrapper/broker hops 1740 с, shim остаётся outer deadline 1800 с | `routers/compose.py`, worker-broker `main.py`, wrapper `broker.py`, `compose_proxy.py` | ФАКТ |
| c | **DONE #681** — Docker events listener переподключается после stream error и clean EOF с bounded backoff | `worker-manager/src/events.py` | ФАКТ |
| d | **PARTIAL #681** — worker-manager `chown -R` и transcript `rglob` вынесены через `asyncio.to_thread`; `subprocess.run` Ansible в coroutine infra-service остаётся | `worker-manager/src/manager.py`; `infra-service/.../ansible_runner.py:200-206` | ФАКТ |
| e | Scaffolder: таймаут не убивает дочерний процесс; потеря lease отменяет главный цикл; inflight-маркер снимается без ACK → дубль сообщения; ошибки full-режима не записывают `scaffold_error` → повтор каждые 30 с | `scaffold.py:51`; `consumer.py:132-138, 158-165, 458-464` | ФАКТ / ГИПОТЕЗА (намерение) |
| f | Read-modify-write всего `project.config` (PO, scaffolder) при PATCH, заменяющем `config` целиком → потеря ключей при конкуренции; аналогично labels серверов | `routers/projects/lifecycle.py:295-296`; `tools_stories.py:216,250-258`; `tools_briefs.py:177-191`; scaffolder `consumer.py:252-255,318-327,391-416`; `infra-service/provisioner/api_client.py:46-57` | ГИПОТЕЗА |
| g | GitHub-клиент: `get_installation_id` не кэшируется (2+ вызова на опрос); постоянный 403 повторяется 3 раза; «Unreachable» достижим после 3 rate-limit ответов | `shared/clients/github/_base.py:86-131, 159-182, 275-291` | ФАКТ |
| h | **DONE #680** — terminal run проверяется до teardown cancellation-check; внешняя отмена run-id → `WorkflowCancelledError` | `shared/clients/github/_actions.py` | ФАКТ |
| i | **DONE #682** — `RedisStreamClient._iter_entries` пробрасывает `CancelledError`; shutdown/`asyncio.timeout()` снова видят cancellation | `shared/redis/client.py`; PR #682 | ФАКТ |
| j | `get_project` PO отдаёт весь `config`, включая шифртекст `config.secrets`, `tree`, `specs_summary`, в контекст LLM и чекпойнты (Architect уже это вырезает, `architect/tools.py:88-90`) | `agents/po/tools_projects.py:187-200` | ФАКТ |
| k | Architect `create_task` принимает `story_id/project_id` от LLM, хотя они есть в `ArchitectState` | `agents/architect/tools.py:131-138` | ФАКТ |
| l | `ResourceAllocatorNode` возвращает ключи `allocation_*`, не объявленные в `DevOpsState` — внутри подграфа они теряются | `resource_allocator.py:81-83,94-96` vs `devops/state.py:22-61` | ФАКТ ключи / ГИПОТЕЗА влияние |
| m | Провижинг-граф с `MemorySaver` накапливает `messages` на каждый триггер (утечка памяти процесса) | `graph.py:54`, `provisioner.py:122-124` | ФАКТ |
| n | N+1: `lk.py:97-124` (3N+1), `/tasks/stats` 11 COUNT (`tasks.py:220-228`), situation-снимок PO — 5 последовательных await (`situation.py:386-390`), `get_story` — run на каждую задачу (`tools_stories.py:545-561`), `allocations.py:229,331`, `_resources_available` на задачу (`supervisor/common.py:52-88`), новый Redis-пул на каждый spawn/send/delete (`worker_spawner.py:826,1036,1151`) | см. | ФАКТ |
| o | Чтения, берущие блокировки записи: `GET …/grant-intents/{id}` и `…/initial-owner-deployment` → `FOR UPDATE` на глобальную строку SystemConfig и story/run | `projects/access.py:325-326, 530-532, 1063, 1158` | ФАКТ |
| p | Гонка порта в `from-repo` → необработанный `IntegrityError` (servers-версия повторяет) | `applications.py:776-800` vs `servers.py:445-448` | ФАКТ |
| q | Редеплой-runs невидимы в `/applications/{id}/runs` (нет `run_metadata.application_id`) | `applications.py:559, 706, 797` | ФАКТ |
| r | Уведомления о провижининге уходят админам дважды (infra-service + бот) | `infra-service/.../handlers.py:282-287`; `telegram_bot/src/notifications.py:84-96` | ФАКТ |
| s | **DONE #681** — wrapper ловит git-pull timeout, декодирует output с replacement и публикует через `-c core.hooksPath=/dev/null` | `worker-wrapper/src/worker_wrapper/wrapper.py` | ФАКТ |
| t | CI: offline `tests/live` запускается дважды (`make test-unit` и `make test-live`); контракт гейта это требует | `ci.yml:225,233`; `test-unit-local.sh:151`; `check-ci-gate.py:664-667,712-731` | ФАКТ |
| u | `make test-clean` не чистит стеки `test-service` (разные имена проектов) | `Makefile:470` vs `:307` | ФАКТ |
| v | `clean_live_tests.py` игнорирует сбои удаления GitHub-репозиториев и не учитывает их в итоговой проверке остатков → «fully complete» при остатках | `clean_live_tests.py:523-578, 127-189` | ФАКТ |
| w | Изменения `scripts/seed_system_configs.py`, `scripts/system_configs*.yaml`, `secrets.yaml` не запускают docker-тесты | фильтры `ci.yml:80-148` | ФАКТ |
| x | Сторонние actions в `deploy.yml`/`stand-e2e.yml` закреплены тегами (в т.ч. `appleboy/ssh-action@v1` с prod SSH-ключом); проверка пиннинга покрывает только `ci.yml` | `deploy.yml:107,115,423,570,577,709`; `stand-e2e.yml:112,117,1252,…`; `check-ci-gate.py:1530-1554` | ФАКТ |
| y | Scaffolder-образ ставит `uv` и `copier` без пина; тестовый образ пинит `copier==9.17.0`, корень требует `>=9.17.1` | `services/scaffolder/Dockerfile:30,35`; `requirements.template.txt` | ФАКТ |
| z | `ttl-sweep` делит concurrency-группу с e2e и может отменить стоящий в очереди dispatch; нет `timeout-minutes` | `stand-e2e.yml:63-66, 1403-1421` | ГИПОТЕЗА |
| aa | Мёртвая DLQ-обёртка/пустые проверки: `pr_poller.py:889`, `story_completion.py:223` (условия никогда не ложны); `deploy.py:471` аннотирован `-> DeployRetryAction`, возвращает bool | см. | ФАКТ |
| ab | **CLOSED #680 — не баг**: `deploy.max_deploy_retries` — число допустимых deploy failures; `attempts >= max` соответствует контракту | `supervisor/deploy.py`, `scripts/system_configs.yaml` | ФАКТ после перепроверки |
| ac | Имя потребителя только по PID (`_base.py:405`) — в контейнерах все PID=1; PO-потребитель уже добавляет hostname (`po.py:154-158`) | см. | ФАКТ код / ГИПОТЕЗА влияние (сейчас 1 реплика) |
| ad | Пустой `apt-get install` в `services/api/Dockerfile.test:9-10` | см. | ФАКТ |
| ae | Инструкции developer-воркера при отсутствии `INSTRUCTIONS.md` молча заменяются пустой строкой / однострочником | `prompts/__init__.py:15-17`, `worker_spawner.py:853-855` | ФАКТ |
| af | Image GC интерпретирует legacy naive UTC timestamp как локальное время: при UTC+2 новая картинка получает возраст +7200 с; актуальные writers уже пишут aware UTC | `worker-manager/src/garbage_collector.py:394-395`, `manager.py:158`; `tests/unit/test_flow.py:149-153`, перепроверено на базе `610c5576` при #685 | ФАКТ код/тест; наличие legacy значений в production не проверялось |
| ag | Пять wrapper HTTP unit tests оставляют реальный `WORKSPACE_DIR=/workspace`: существующий каталог без Makefile завершает turn до fake agent/HTTP, хотя fixtures перенаправляют TASK/STORY paths | `packages/worker-wrapper/tests/conftest.py:6-10`, `tests/unit/test_http_integration.py`; `wrapper.py:987-992`, перепроверено при #685 | ФАКТ; дефект изоляции тестов, производственный fail-fast сохраняется |

---

## 10. Укрупнённая нарезка: весь аудит в 12 PR

Цель этой нарезки — не делать PR на каждый пункт аудита. Один PR закрывает одну крупную границу
ответственности и забирает соседние баги/дедупликации, если они имеют тот же failure domain.
Ориентир остаётся **12 PR на весь аудит**, включая уже завершённые первые семь итераций. PR #686 — предварительное
сжатие CHANGELOG и в счётчик не входит. После #687 осталось **5 аудиторских итераций; следующая — PR 8,
Scheduler/runtime simplification**. Остаток сгруппирован так,
чтобы correctness/contract work не смешивался с giant-file mechanics, а
финальный platform sweep не поглощал локальные scheduler/LangGraph/API проблемы. Низкоприоритетные
наблюдения не получают отдельного PR: они входят в ближайший тематический кластер либо закрываются
явным решением «не делать».

| PR | Статус | Кластер | Что входит | Основные ограничения / приёмка |
|---|---|---|---|---|
| 1 | **DONE — #679** | **Deploy handoff correctness** | A5 + §9.11: единый scheduler deploy handoff; стабильные logical-attempt Run id; exact message в Run; create Run до Story transition; queued-handoff recovery | PR CI green; post-merge CI green; 0 random scheduler deploy ids на этих путях |
| 2 | **DONE — #680** | **Deploy execution safety** | §9.1 fail-closed при записи GitHub secrets; §9.2 owner-fenced atomic release deploy lock; §9.16h cancellation/wait terminal guards; §9.16ab перепроверен и закрыт как не-баг | полный PR CI green; LangGraph service tests green; Required CI Gate green |
| 3 | **DONE — #681** | **Worker lifecycle & workspace safety** | §9.3 live worker-stream cleanup fence; §9.5 project_id/repo_id workspace GC; §9.16b/c; worker-manager часть §9.16d; §9.16s wrapper robustness | полный PR CI + Required CI Gate green; correctness отделён от механической декомпозиции |
| 4 | **DONE — #682** | **Queue delivery semantics** | A8 + §7.2; §9.6 invalid provisioner poison; §9.10 transient API ACK; §9.16i CancelledError; terminal DLQ→ACK + durable delivery ceiling | полный PR CI green; LangGraph service suite + Required CI Gate green; механический StreamCodec вынесен из correctness boundary |
| 5 | **DONE — #683** | **API authorization boundary** | §9.4: LK JWT допускается только на явно bearer-aware routes; internal-only default для неразмеченных endpoints; registry regressions | полный PR CI green; API + LangGraph service suites + Required CI Gate green; /lk/* и explicit admin/owner bearer flows совместимы |
| 6 | **DONE — #685** | **DB + Run contract correctness** | A9 + A10 + §9.9: все 14 drift differences устранены; incident indexes сохранены; guarded migration; Run enums/CHECK и reject explicit null | полный PR CI + post-merge CI green; `compare_metadata == []`; HTTP/DB vocabulary и partial-index behavior проверяются в service suite |
| 7 | **DONE — #687** | **Documentation/context reduction** | A1 + остаток A2 после #686; D1–D5; CONTRACTS index + 8 `docs/contracts/*`; CHANGELOG demand-driven; SECRETS operational runbook split; NODES/LOGGING/architecture drift cleanup | CI green; `CONTRACTS.md` ~191k→~28k chars; default reading = index + relevant guide; runtime behavior unchanged |
| 8 | **OPEN — NEXT** | **Scheduler/runtime simplification** | A3 periodic loop; A11; scheduler-часть A4/§6.2 client dedupe; разрез `supervisor/deploy.py` и `pr_poller.py`; §9.16a/aa; scheduler-local N+1 из n | log event names/state-machine order invariant; full scheduler tests; extraction идёт после characterisation, основные orchestration modules ≲600 LOC где разумно |
| 9 | OPEN | **LangGraph boundary cleanup** | разрез `_qa_runner.py`, architect/qa/deploy/worker_spawner/deployer; доказанный LangGraph dead code A13/A14; §9.16j/k/l/m/ac/ae где относится к agent runtime; LangGraph-local N+1 | не тащить secrets в LLM/checkpoints; без compatibility re-export; patch targets/codemod обновлены; service tests + consumer invariants green |
| 10 | OPEN | **Worker contracts + decomposition** | A7 typed `WorkerTurnInput`; удалить мёртвый `developer_worker.py`; A12 builders для worker/workspace/po-response keys; worker dead DTO/code; §9.16af legacy UTC image-GC и ag workspace fixture isolation; перенесённый из #681 разрез `worker-manager/manager.py` и `worker-wrapper/wrapper.py` | Review Trigger для shared/contracts; producer→broker→wrapper serialization; характеризация времени/WORKSPACE_DIR перед extraction; существующие lifecycle fences сохранены |
| 11 | OPEN | **API domain correctness + extraction** | A6; helpers из routers/; разрез `projects/access.py`, stories/_story_actions, servers, runs, applications; §9.7/9.12, o/p/q; §9.16f для project config/labels с cross-service characterization | lock ordering неизменен; RMW не теряет concurrent config keys; 0 функционально-локальных imports `routers.*` из admission; API service tests green |
| 12 | OPEN | **Platform/CI/infra residual sweep** | A16–A21; оставшийся §6.6 dead code; compose/Makefile/check-ci-gate/live-harness splits; механический `shared/redis/client.py` → validation/DLQ + `StreamCodec`; infra Ansible async часть §9.16d; scaffolder §9.16e/y; GitHub-client g; notification r; CI t/u/v/w/x/z/ad; frontend contract drift | correctness fixes идут перед mechanics; async/process boundaries characterization; `make ci-contract`, normalized compose, scripts/frontend tests; A21: operator logic живёт в `.py/.sh`, docs только ссылаются на него; если meaningful diff приближается к ~2k, низкоприоритетную механику явно defer/no-do вместо искусственного 13-го PR |

### 10.1 Что изменилось относительно исходного плана

- Старые шаги 0–14 были безопасной последовательностью техник, но не реалистичной PR-нарезкой:
  только giant-file splits там фактически означали много отдельных PR.
- Новая нарезка считает один failure domain одной итерацией и складывает туда его баги,
  дедупликации и декомпозицию. Поэтому A5 и §9.11 закрылись одним PR #679, deploy execution
  safety (§9.1, §9.2, §9.16h) — PR #680, worker lifecycle correctness — #681, queue delivery
  correctness — #682, API authorization boundary (§9.4) — #683, DB + Run contract correctness — #685;
  §9.16ab после проверки закрыт как не-баг. Documentation/context reduction закрыт #687; обнаруженный при нём A21
  не требует отдельной итерации и включён в PR 12.
- P0/P1 correctness и security идут раньше чистого уменьшения LOC/контекста, кроме документации:
  docs вынесены в отдельную раннюю итерацию, потому что они увеличивают стоимость каждой последующей работы.
  Перед ней #686 отдельно сжал CHANGELOG ≈5× по прямому решению владельца; это не новая 13-я итерация, а уменьшение
  скоупа PR 7. #687 завершил Navigation/default-reading cleanup и CONTRACTS split; дальнейший docs residual — только A21 в PR 12.
- PR #681 подтвердил полезность отдельного correctness boundary: механический разрез `manager.py`/`wrapper.py`
  не понадобился для исправлений и перенесён в PR 10 рядом с worker contracts/key vocabulary; оставшийся
  blocking Ansible относится к infra и перенесён в PR 12.
- PR #682 повторил тот же принцип: correctness queue semantics закрыты без рискованного механического
  `StreamCodec` refactor. Теперь этот split опирается на характеризационные/service tests и живёт в PR 12
  рядом с остальными shared/infra механическими разрезами.
- После #683 локальные residual findings вынесены из бывшего «всё остальное» PR 12 к владельцам failure
  domain: scheduler N+1 — в PR 8, agent-runtime j/k/l/m — в PR 9, config/port/redeploy correctness — в PR 11.
  PR 12 оставлен только для platform/CI/infra/shared mechanics и связанных correctness gaps.
- PR #685 закрыл DB/Run correctness без декомпозиции API или общей замены DTO. PR #687 затем закрыл
  документацию/context reduction; следующая итерация — scheduler/runtime simplification (PR 8); оставшиеся `RunDTO.project_id`, result DTO и прочие HTTP contract findings
  из §7.3 не объявлены исправленными и остаются у соответствующих API/LangGraph границ.
  Дополнительно подтверждённые legacy UTC/fixture issues относятся к worker PR 10 и не создают новую итерацию.
- Shared-contract и DB изменения не смешиваются с giant-file mechanics: PR 6 выполнен с отдельной
  guarded migration/rollback boundary; для PR 10 сохраняется Review Trigger. Финальный ориентир остаётся 12 PR; если PR 12 приблизится к
  ~2k meaningful LOC, низкоприоритетная механика должна быть явно deferred/no-do, а не раздувать sweep
  или автоматически создавать 13-ю итерацию.

## 11. Неопределённости, ограничения и намеренно отвергнутые абстракции

### 11.1 Ограничения аудита

- Исходный аудит — статический, кроме временного Postgres для сравнения миграций с моделями. Закрытия #679–#687 подтверждены соответствующими тестами/CI, перечисленными в §1.2.1; это не повторный runtime-аудит всех оставшихся пунктов.
- Локальный broad-прогон #685 не был зелёным: отсутствовали Docker/привилегированные OS-возможности, LangGraph fixtures предполагали доступные non-root UID; отдельно локализованы §9.16af/ag. Merge подтверждён полным зелёным GitHub CI и post-merge CI, а не успехом локального broad.
- Содержимое `tests/live/*` (~48 k строк) детально не ревьюилось — оценены только размер и роль.
- Оценки токенов — chars/4 без реального токенайзера (±20–25%); оценки LOC — по AST/строкам целевых диапазонов, без фактического выполнения рефакторинга.
- Баги с пометкой ГИПОТЕЗА (9.16e/f/z/ac и др.) требуют рантайм-подтверждения; после #683 наиболее важным из них остаётся 9.16f (потеря ключей `config`).
- Внешние потребители API (кроме фронтендов и сервисов репозитория) не известны — влияет на безопасность удаления `PUT /api/projects/{id}` и типизации `RunUpdate`.
- Не проверялись: продуктовый шаблон `codegen-product-kit`, содержимое GitHub-секретов/окружений, производственные данные (наличие строк с `run.project_id IS NULL` и т.п.).

### 11.2 Намеренно отвергнутые абстракции

- **Единый «универсальный» API-клиент для всех сервисов** — отвергнут; предложен только read-mixin для доказанно идентичных методов. Методы с аудит-идентичностью (`transition_story`, `get_project` с `X-Telegram-ID`) остаются локальными.
- **Слияние PO-цикла с `run_queue_worker`** — отвергнуто (разные требования: per-chat блокировки, DLQ, отсутствие live-work lease).
- **Общий npm-пакет для двух фронтендов** — отвергнут (выгода ~20 строк).
- **Генерация TS-типов из OpenAPI** — предложена как опция P3, а не обязательный шаг: требует build-шага и CI-проверки свежести.
- **Единый парсер GitHub URL** — не считается «безопасной» дедупликацией: 5 реализаций ведут себя по-разному на нестандартных входах; сначала характеризация.
- **Замена `StoryRead/TaskRead/RunRead` на shared DTO** — отдельно отложена: #685 закрывает только Run vocabulary (A10), сохраняя существующие различия nullability/result. Общая замена требует собственной характеризации persisted values и HTTP-совместимости.
- **Перенос `worker_compose.py`, live-harness модулей и `qa_capabilities` из `shared`** — отвергнут/отложен: они исполняются внутри контейнеров (`docker exec … python -m`), либо описаны как контракт.
- **Удаление моста provisioner (`provisioner:trigger` → одноузловой граф)** — не рефакторинг, а изменение поведения (дедупликация, долговечность); вынесено как отдельное решение владельца.
- **Дедупликация повторяющейся фразы в PO-промпте** — требует продуктового решения, закреплена тестами.
- **Удаление легаси-Ansible (`site.yml` и роли)** — только после подтверждения владельца, что набор не используется вручную.
