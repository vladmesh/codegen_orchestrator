# Sprint 002: Thermo-Nuclear Hardening

> **Goal**: Закрыть находки thermo-nuclear-review — типизированные границы (enums на чтении, а не только на записи), fail-fast вместо тихих ошибок, удаление мёртвых слоёв. Детали по каждому пункту — [docs/thermo-nuclear-review.md](../../thermo-nuclear-review.md).
> **Type**: tech
> **Started**: 2026-07-01

Легализует дрейф: работа по ревью шла ad-hoc ветками (PR #20–24) мимо спринт-фреймворка, пока Sprint
001 стоял замороженным с апреля. Этот спринт сводит её обратно в процесс.

## Phase 0: Security quick-wins ✓ (COMPLETE)
- B2 crypto: `InvalidToken` пробрасывается, plaintext-fallback и `value_prefix`-лог убраны — thermo §B2 (`9c060007`)
- api fail-open auth: `X-Internal-Key`/`INTERNAL_API_KEY`, `require_internal_or_admin`, 4 internal-клиента обновлены — thermo §"api fail-open auth"

## Phase 1: Остаток security-блокеров
- **[P0, первым] Разблокировать CI.** `main` красный на `ruff format --check`: 5 файлов из #24 (audit_fixes) влиты неотформатированными — `services/api/src/dependencies.py`, `routers/projects.py`, `routers/runs.py`, `services/scheduler/tests/unit/test_api_client.py`, `shared/crypto.py`. Фикс: `uv run ruff format . && git commit`. Пока main красный, остальное сливать вслепую.
- B1 scaffolder exec: добить неполный фикс (#20 закрыл только project_name/modules) — `create_subprocess_exec` с arg-list, `template_repo`/`repository_id` больше не через shell — thermo §B1
- scaffolder token-in-URL: `GIT_ASKPASS`/`http.extraHeader`, редакция git stderr перед сохранением в `scaffold_error` — thermo §Security п.4

## Phase 2: Затянуть контракты shared/ (keystone) ✓ (COMPLETE)
- B7: response-DTO на enums вместо `str` (task/story/server/application/incident/service_deployment) — thermo §B7 (`codegen_orchestrator-435`)
- Дублирующиеся словари: единые `AgentType` / `ActionType` / `ResultStatus`+`LifecycleEvent` — thermo §"Duplicated vocabularies" (`codegen_orchestrator-436`)
- Типизированный `RunResult` (per-`RunType` union, привязан к `type`) вместо `run.result: dict | None` — thermo §P1 `run.py:39` (`codegen_orchestrator-440`)

## Phase 3: Типизированный consume + удаление мёртвых слоёв
- `consume_typed` в redis-клиенте; приватизировать raw `publish`; `JSONDecodeError` не глотать — thermo §P1 `client.py`
- B6 worker result: контракт `WorkerResult`, `publish_message`, `_to_spawn_result` вместо ручных dict'ов в 3 местах — thermo §B6
- engineering consumer + scheduler `run.result` на `model_validate` — thermo §P1
- B5: удалить мёртвый `langgraph/src/tools/` (~800 строк, шадовит живые тулы) — thermo §B5
- `worker:lifecycle` стрим, `agent_config_cache`, `shared` compat-shims — удалить — thermo §"Dead code"

## Phase 4: Тихие ошибки → fail-fast
- B3 infra incidents: реализовать 3 метода клиента или удалить подсистему; убрать `try/except → False` обёртки — thermo §B3
- B4 secret_resolver: `raise` вместо фейковых значений, убрать `"unknown"` sentinel — thermo §B4
- Swallow-list: rag_ingest (embedding=None), worker-wrapper writes, po tools, architect `"422" in str(e)`, `_base` poison-loop, infra api_client, `release_allocation`, notifications — thermo §"fail-fast violations"
- Magic-number config fallbacks (supervisor/worker-manager/api_types) — thermo §там же

## Decisions
- **Sprint 001 припаркован.** Его Phase 0/1 (github split #19, ddgs-rename, noqa) сделаны; Phase 2
  (порты, шифрование ключей) поглощена этим спринтом и бэклогом. В Sprint History занесён как partial.
- **Порядок фаз — из «recommended sequencing» ревью**: security → контракты (keystone) → typed
  consume + мёртвый код → тихие ошибки. Затягивание типов у источника удаляет защитные ветки в 7
  сервисах, а не переставляет их (оценка ревью — минус ~1500 строк).
- **Задачи фаз 1–4 генерятся перед началом фазы** (`/plan-phase`); детальные спеки — в
  `thermo-nuclear-review.md`, дублировать в task-файлы заранее не нужно.

## Deferred (в бэклог / следующий tech-спринт)
- P2 abstraction extractions: api task state machine, router scaffolding, дублирующиеся wire-схемы,
  scheduler retry/timeout, два API-клиента langgraph, worker agent-type, deploy epilogue, dashboard URL.
- P2 file-splits: `wrapper.py` (893), `supervisor.py` (642), `telegram main.py` (531), `graph.py` `OrchestratorState`.
- P2 non-atomic state: supervisor retry, `delete_worker` lock leak, worker-manager status source, `complete_task` фейковые события.
- Lower-priority nits (thermo §"Lower-priority nits").

## Endgame
- Audit: pending
- E2E: pending
- Fix phase: pending
- Docs: pending
