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
- [x] B7: response-DTO на enums вместо `str` (task/story/server/application/incident/service_deployment) — PR #35, `codegen_orchestrator-435`, thermo §B7
- [x] Дублирующиеся словари: единые `AgentType` / `ActionType` / `ResultStatus`+`LifecycleEvent` — PR #36, `codegen_orchestrator-436`, thermo §"Duplicated vocabularies"
- [x] Типизированный `RunResult` (per-`RunType` union, привязан к `type`) вместо `run.result: dict | None` — PR #38, `codegen_orchestrator-440`, thermo §P1 `run.py:39`

## Phase 3: Типизированный consume + удаление мёртвых слоёв ✓ (COMPLETE)
- [x] `consume_typed` в redis-клиенте; `JSONDecodeError` не глотать — PR #40, thermo §P1 `client.py`
- [x] B6 worker result: контракт `WorkerResult`, `publish_message`, `_to_spawn_result` вместо ручных dict'ов в 3 местах — PR #41, thermo §B6
- [x] engineering consumer через `EngineeringMessage.model_validate` + scheduler `run.result` на типизированные атрибуты — `codegen_orchestrator-457` (engineering) / PR #38, #40 (scheduler), thermo §P1
- [x] B5: удалён мёртвый `langgraph/src/tools/` (projects/servers/github/specs + dead result models в `schemas/tools.py`); живой `allocator` перенесён в `langgraph/src/allocations.py` без eager-import мёртвых модулей — `codegen_orchestrator-457`, thermo §B5
- [x] `worker:lifecycle` стрим+контракт, второй `agent_config_cache`, `scaffold_phase.py`, `shared` compat-shims (RedisStreamClient try/except, ServiceDeployment/DeploymentStatus алиасы, legacy DeploymentStatus enum, ensure_consumer_groups) удалены — `codegen_orchestrator-457`, thermo §"Dead code"
- Приватизация raw `publish`/`publish_flat` НЕ входит в срез: методы держат ~13 живых production call sites (callback-события, PO input/proactive, provisioner/worker responses) — миграция на `publish_message` идёт по consumer'ам в Phase 3/4, raw API не расширялся.

## Phase 4: Тихие ошибки → fail-fast ✓ (COMPLETE)
- [x] B3 infra incidents: атомарный лимит provisioning attempts (`codegen_orchestrator-464`) и
  incident journal reconciliation (`codegen_orchestrator-466`). Successful provisioning writes
  `READY` before journal closure; temporary closure failure remains observable and scheduler resolves
  only active `provisioning_failed` incidents for confirmed `READY` servers, without recovery work.
- [x] B4 secret_resolver (`codegen_orchestrator-473`): обязательный project context, allocation и repository metadata валидируются до deploy; фейковые значения и `"unknown"` sentinel удалены, persistence failures идут в deploy error path.
- Swallow-list:
  - [x] rag_ingest embeddings, worker-wrapper TASK.md/STORY.md writes, PO repository/spec writes and `release_allocation` (`codegen_orchestrator-476`)
  - [x] architect `"422" in str(e)`, `_base` poison-loop, infra api_client, notifications (`codegen_orchestrator-477`) — thermo §"fail-fast violations"
- [x] Magic-number config fallbacks (supervisor/worker-manager/api_types) — `codegen_orchestrator-489`, thermo §там же

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
- Audit: complete: [original RED audit](../../reports/sprint-002-closeout-audit.md) and
  [rerun](../../reports/sprint-002-closeout-audit-rerun.md),
  **GREEN — Stage 4 exit gate выполнен**
- E2E: pending
- Fix phase: complete
- Docs: complete
