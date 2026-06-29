# Thermo-Nuclear Code Quality Review — full codebase

- **Date:** 2026-06-30
- **Branch:** `vladmesh/seaslug` (even with `origin/main` @ `277fe924`; no branch-local diff, so this reviews the codebase as it stands)
- **Method:** 7 parallel deep-review passes (api, langgraph-execution, langgraph-agents, scheduler, worker-manager+wrapper, shared, smaller services). Read-only. Applied thermo-nuclear standards plus the repo's own anti-patterns from CLAUDE.md (fail-fast/no-fallbacks, enums-and-contracts/no-raw-dicts, canonical-layer reuse).
- **Scope:** all `services/*/src`, `packages/worker-wrapper/src`, `shared/`. Tests read only where they exposed a design smell. `admin-frontend`/`user-dashboard` have no Python (skipped).

## Overall health

The architecture is sound: clean service boundaries, no file over 1000 lines, sensible graph/consumer topology, shell handling in the worker path is injection-safe. The problems are not architectural drift, they are two systemic patterns repeated everywhere, plus several layers of dead code.

**Two dominant themes account for ~70% of findings:**

1. **The typed boundary holds on write and dissolves on read.** `shared/` defines mature enums and Pydantic contracts, request DTOs use them, `publish_message` is typed. But response DTOs declare `status`/`type` as bare `str`, `run.result` is `dict | None`, queue *consume* yields raw `dict`, and every consumer then hand-parses with `.get(key, default)` and multi-guess string comparisons. The single-source-of-truth guarantee evaporates the moment a message is read. This is the direct inverse of the repo's "enums and schemas, never hardcoded strings or dicts" rule.

2. **Silent failure instead of fail-fast.** Across crypto, secret resolution, RAG ingest, infra incidents, worker file writes, and auth, errors are swallowed (`try/except → return None/[]/False`) or papered over (`x or fallback`, `.get(k, default)`). The repo's #1 anti-pattern is "fail-fast, no fallbacks", and it is violated in load-bearing places that cause invisible data corruption or wedge state.

Fixing these two themes deletes real code (an estimated 1500+ lines of dead/duplicated/defensive code) while making behavior *more* correct, not just cleaner. That is the code-judo win here.

---

## P0 — Blockers

These either cause silent incorrectness, ship a known security hole, or carry a whole dead layer that shadows live code.

### B1. Scaffolder shell-injection fix is incomplete — two of four exec inputs still unguarded
- **`services/scaffolder/src/scaffold.py:124,89,233`**, `_run_cmd:31-40`
- The recent fix (PR #20) validates only `project_name` and `modules`, but `run_scaffold` also interpolates `template_repo` and the `repository_id`-derived `workspace` path straight into `create_subprocess_shell` strings. By the fix's own threat model (ScaffoldMessage fields forwarded straight through the queue), `repository_id="x; rm -rf /"` or a `template_repo` containing `$(...)`/backticks still injects.
- **Remedy:** Convert `_run_cmd` to `asyncio.create_subprocess_exec` with argument lists (split the `&&` git chains into sequential exec calls). This deletes the entire shell-metacharacter class and demotes `validation.py` to defense-in-depth instead of the only barrier.

### B2. `SecretsCipher.decrypt` swallows `InvalidToken`, returns ciphertext, and logs a secret prefix
- **`shared/crypto.py:44-55`**
- On decryption failure it logs `value_prefix=ciphertext[:10]` and returns the input unchanged ("gracefully handles plaintext values"). Three problems in one: a fail-fast violation, a backward-compat shim for un-migrated plaintext (forbidden in this prototype), and a log that leaks the first 10 chars of a value that may itself be a plaintext secret. Callers then treat still-encrypted bytes as "decrypted".
- **Remedy:** Let `InvalidToken` propagate; delete the plaintext fallback and the `value_prefix` log field.

### B3. infra-service incident subsystem is silently dead
- **`services/infra-service/src/provisioner/incidents.py:29,66,77`** call `api_client.create_incident`/`list_incidents`/`update_incident`; **`services/infra-service/src/clients/api.py:19-77`** defines none of them.
- Every incident call raises `AttributeError`, caught by a blanket `except Exception` and logged as `incident_create_failed`. No incident is ever created on provisioning failure or resolved on recovery. The whole subsystem no-ops invisibly.
- **Remedy:** Implement the three client methods, or delete `incidents.py`. Remove the `try/except → return False` wrappers so a missing method crashes loudly.

### B4. Secret resolver bakes silently-wrong values into the deployed `.env`
- **`services/langgraph/src/subgraphs/devops/secret_resolver.py:184`** (port→`"8000"`), `:199` (image→`unknown/unknown-service:latest`), `:164,167,176` (project name→`"app"`/`"project"`/`"value"`), `:31,93` (project_id→`"unknown"` sentinel)
- When an allocation, repo URL, or project name is missing, the node fabricates plausible-but-wrong values and ships them in the encoded DOTENV / deploy secrets. The deploy then "succeeds" against garbage config, surfacing the error far downstream.
- **Remedy:** Raise when the allocation/name/repo is absent. Drop the `"unknown"` project_id sentinel and the `project_id != "unknown"` guard.

### B5. Dead legacy `langgraph/src/tools/` layer shadows the live agent tools and is still loaded
- **`services/langgraph/src/tools/{projects,servers,github,specs}.py`**, `tools/__init__.py`, plus dead result models in `schemas/tools.py`
- Live agents build tools from `agents/po/tools.py` and `agents/architect/tools.py`. Nothing imports the old `create_project`/`list_projects`/etc. Only `tools/allocator.py` (and `tools/github.get_github_client`) is live. But `nodes/resource_allocator.py`/`consumers/deploy.py` import `..tools.allocator`, which runs `tools/__init__.py`, which eagerly imports all four dead modules and their deps (`ddgs`, `yaml`, github client). ~800 lines load and shadow the real implementations (two `create_project` with different ID schemes).
- **Remedy:** Delete `tools/{projects,servers,specs}.py`, the unused `github.py` tools, and the dead result models. Move `allocator.py` + `get_github_client` out so `__init__.py` stops re-exporting the dead set.

### B6. Worker result crosses the wrapper→langgraph boundary as an untyped raw dict
- Producer **`packages/worker-wrapper/src/worker_wrapper/wrapper.py:115-118,188-215`** + `http_models.py:44-60`; consumer **`services/langgraph/src/clients/worker_spawner.py:262-333,441-461`**
- The result is hand-built dicts with hardcoded statuses (`{"status":"failed"/"completed"/"blocked"}`) published via `redis.publish`. The consumer guesses across synonym keys: `status in ("success","completed")`, `content / response / output`, `block_reason or reject_reason`. `shared/schemas/worker_events.py` already defines `WorkerCompleted`/`WorkerFailed` with typed `commit_sha`/`files_changed`, and `BLOCKED` is in the task enum. Neither is used. The ~30-line output→`SpawnResult` block is also duplicated verbatim in `request_spawn` and `send_task_to_worker`.
- **Remedy:** Define one `WorkerResult` Pydantic contract in `shared/contracts/queues/`, publish with `publish_message`, parse with a `TypeAdapter`. Extract `_to_spawn_result(resp) -> SpawnResult` for both call sites. Deletes `to_redis_output`, the synonym `.get()` chains, and the manual dict assembly in three places.

### B7. Response DTOs are stringly-typed: enums exist but `status`/`type`/`role` declared `str`
- **`shared/contracts/dto/`**: `task.py:85,89,110-112`, `story.py:81-82`, `server.py:46,88`, `application.py:35,52,58`, `incident.py:38-39,54,62`, `service_deployment.py:14`
- Every response DTO declares its lifecycle field as bare `str` even though the matching `StrEnum` sits in the same file. Request DTOs use the enums, so the boundary is typed inbound and untyped outbound. Two sites even advertise it: `status: str = "discovered"  # Use str for flexibility`. This is the root cause of the stringly-typed reads scattered through every service.
- **Remedy:** Change every `status: str`/`type: str` to the existing enum; delete the "flexibility" comments. Pydantic then rejects unknown values at the boundary, which lets the downstream `if status in (...)` branches across services disappear.

---

## P1 — Systemic theme: typed boundary erosion

All of these are instances of the same root cause as B6/B7. Fix the contracts once, then these become mechanical.

| Where | Problem | Fix |
|---|---|---|
| `langgraph/consumers/engineering.py:89-98` | Hand-unpacks 11 fields via `job_data.get("action","create")` while deploy/qa/architect use `Msg.model_validate`. `EngineeringMessage` exists, unused here. | `EngineeringMessage.model_validate(job_data)` |
| `shared/contracts/dto/run.py:39` | `run.result: dict \| None` actually carries deploy/QA/engineering payloads by `RunType`; permits a QA outcome on a deploy run. Read with `.get()` guessing in scheduler + langgraph. | Typed `RunResult` discriminated union keyed off `RunType` (reuse `DeployOutcome`/`QAOutcome`) |
| `shared/redis/client.py:95-256` | `publish_message` typed, but `publish`/`publish_flat` are raw escape hatches and `consume()` yields `dict[str, Any]`. `_parse_fields` swallows `JSONDecodeError → pass`. | `consume_typed(stream, group, consumer, model) -> AsyncIterator[T]`; privatize raw publish; let JSON failures raise |
| `scheduler/supervisor.py:288-616` | `run.result or {}` then `.get("deploy_outcome","")` then `DeployOutcome(x)` in `try/except ValueError: continue`. | Model `DeployResult`/`QAResult`; `model_validate`; attribute access |
| `langgraph/subgraphs/devops/smoke.py`, `deployer.py` | Check/result dicts with invented `pass/fail/skip/success/error` strings read back stringly. | `SmokeCheck`/`SmokeResult` + `CheckResult` enum |
| `langgraph/consumers/_events.py` + ~12 sites | Callback event kind is a bare `str` (`"progress"` etc.); PO re-matches an inline `_STORY_EVENTS` set. | `CallbackEvent` StrEnum, type `POSystemEvent.event` |
| `langgraph/consumers/engineering_result_handler.py:99,131,228,271` | Bare `"failed"`/`"completed"` while deploy siblings use `RunStatus.*.value`. | Use the enum |
| `langgraph/subgraphs/devops/env_analyzer.py` + `secret_resolver.py` | `"infra"/"computed"/"user"` bare strings across producer/consumer, documented only in a comment. | `EnvVarClass(StrEnum)` |
| `api/routers/servers.py:245-307` | `update_server(updates: dict)` with a hand-maintained `allowed_fields` whitelist + manual datetime parsing + `hasattr` guards. | `ServerUpdate(BaseModel)` + `model_dump(exclude_unset=True)` setattr loop (pattern already in `repositories.py:122`) |
| `telegram_bot/main.py:282-296` | Reads PO reply as raw dict, detects errors via magic `error == "true"` while `POResponse.error` is `str \| None`. | `from_flat_fields(data, POResponse)`; make `error` a `bool` |
| `shared/contracts/dto/project.py:52,63,65` | `project_spec: dict` though `ProjectSpecYAML` schema exists; `application.py:40 ports: list[dict]`. | Type `project_spec` as `ProjectSpecYAML`; add `PortMapping` |
| api composite endpoints (`_task_actions.py:195,257`, `applications.py`, `servers.py:328`) | Return ad-hoc dicts with no `response_model`; `get_server_incidents` rebuilds `IncidentRead` by hand. | Small response schemas + `response_model=` |
| `langgraph/clients/worker_spawner.py:265-270` | Create ACK read via `.get()` though `CreateWorkerResponse` exists. | `CreateWorkerResponse.model_validate` |

**Duplicated vocabularies** (same concept, multiple disagreeing definitions, all in `shared/`):
- *Agent type:* `AgentType` enum (claude/factory/noop) vs `agent_config.py` `Literal["claude","factory"]` vs `worker_events.worker_type Literal["droid","claude_code","codex"]` vs `config.py default="claude"`. Make `AgentType` the single source. Worker-side code (`wrapper.py`, `manager.py:295`, `container_config.py:48`, `image_builder.py`) also compares raw `"claude"` strings.
- *Action:* `EngineeringMessage.action` inline `Literal["create","feature","fix"]` vs `DeployAction` enum vs `TaskType` enum. Define one `ActionType`.
- *Result/lifecycle status:* ≥5 inline Literal sets (`base.py:27`, `events.py:10`, `worker_lifecycle.py:11`, `worker_events.py:15`, `worker.py:77,113`); `BaseResult.status` even carries both `"failed"` and `"error"`. Define `ResultStatus` + `LifecycleEvent` enums; collapse the failure synonyms.

---

## P1 — Systemic theme: fail-fast violations / silent failures

Beyond the blockers (B2/B3/B4), the swallow-and-continue pattern recurs:

- **`api/routers/rag_ingest.py:128-164`** — `generate_chunk_embeddings` catches all exceptions, returns `[]`, then `upsert_document` stores chunks with `embedding=None` and reports success. RAG search silently misses those docs. **Data corruption.** Let it propagate.
- **`api` fail-open auth** — `projects.py:43-44`, `runs.py:38-39`, `servers.py:29-56`: a missing `X-Telegram-ID` header means "allow". This exposes `GET /servers/{handle}/ssh-key` (decrypted SSH private key, `servers.py:125-140`) and all server mutations to any caller that omits the header. **Security.** Make internal access an explicit positive signal (service token), not absence of a header; consolidate into `dependencies.py` (the unused `require_admin`/`get_current_user` already live there).
- **`worker-wrapper/wrapper.py:529-547`** — `_write_task_md`/`_write_story_md` catch `OSError` and warn. The agent then runs against the *previous* `TASK.md`, doing the wrong work and burning credits silently. Let correctness-gating writes propagate.
- **`langgraph/agents/po/tools_projects.py:79-84`** + `tools_stories.py:103-120` — repository-record create and spec-persist wrapped in `try/except → warning`; the repo record is what `scaffold_trigger` keys on, so the project silently never scaffolds while the tool reports success.
- **`langgraph/agents/architect/tools.py:203-211`** — detects "already in state" via `if "422" in str(e)` over a stringified exception. Catch `httpx.HTTPStatusError`, branch on `e.response.status_code`.
- **`langgraph/consumers/_base.py:74-75,148-154`** — job-loop exception neither ACKs nor dead-letters; with `claim_pending=True` a poison message re-runs forever. Pick one policy (ACK + terminal failure, or DLQ after N).
- **`infra-service/src/provisioner/api_client.py:20-158`** — every method is `try/except Exception → return None/False/[]`. Callers proceed on bad data; this is what hides B3.
- **`langgraph/clients/api.py:285-291`** — `release_allocation` swallows `HTTPStatusError → False`.
- **`shared/notifications.py:179-188,199`** — `notify_admins` hand-parses `/api/users` as raw dicts and `except Exception: return 0`, masking a down API as "0 admins notified".
- Magic-number config fallbacks: **`scheduler/supervisor.py:33-58`** (`..._config.get_int(key) if _config else N`, all dead since `init_config` always runs); **`worker-manager/manager.py:421-422`** (`settings.WORKER_REDIS_URL or "redis://redis:6379"`); **`langgraph/schemas/api_types.py:81-83`** (`public_ip or host`).

---

## P1 — Security (consolidated)

The security-relevant items, gathered for triage:
1. **B1** scaffolder shell-injection (incomplete fix) — highest.
2. **B2** crypto returns ciphertext + logs secret prefix.
3. **api fail-open auth** exposes decrypted SSH private keys (above).
4. **`scaffolder/src/scaffold.py:96,99,256`** — GitHub token embedded in clone URL (`https://x-access-token:{token}@...`); on a failed git op the credentialed URL lands in `result.error`, gets logged, and is persisted to the project's DB config as `scaffold_error` (`consumer.py:170-171,230-231`), later shown to users. Use `GIT_ASKPASS`/`http.extraHeader`; redact git stderr before storing.

---

## P2 — Dead code to delete

Straight deletions, no behavior change, each removes a thing a reader must currently reason about:

- **B5** dead `langgraph/src/tools/` layer (~800 lines) + dead result models in `schemas/tools.py`.
- **`worker:lifecycle` stream** — no consumer anywhere. Delete `publish_lifecycle` + 3 call sites (`wrapper.py:99,119,167,883-893`) + `WorkerLifecycleEvent` contract; `_publish_result` then returns nothing.
- **`langgraph/config/agent_config_cache.py`** — a second `AgentConfigCache` stacked on top of `agent_config.py`'s cache (its `.get()` calls `get_agent_config()` which calls the first cache). Delete it; point callers at `agent_config.get_agent_config`.
- **`langgraph/nodes/developer.py:353-377`** — 7 identity pass-through methods kept "so tests calling `node._method` keep working". Point tests at `developer_tasks` functions; delete wrappers + `__all__` shim.
- **`langgraph/state/context.py`** — global-mutable context, effectively unused.
- **`worker-wrapper/wrapper.py:742-745`** — `_collect_and_archive` never called in production; tests patch it as a no-op.
- **`api/routers/tasks.py:50-82`** — underscore alias block + oversized `__all__`; nothing imports the underscore names.
- **`scheduler/supervisor.py:528`** — `MAX_QA_LOOPS = 2` never referenced; `_handle_qa_failed` always returns `True`, so the `else: failed += 1` branch is dead.
- **`infra-service/src/provisioner/node.py:42-49,394-395`** — backward-compat `__all__` re-exports + unused module singleton; **`api_client.py:135-158`** `increment_provisioning_attempts` has no callers, so the max-retry guard at `node.py:103` can never trip; **`ansible_runner.py:172-174`** dead alias.
- **`shared`** compat shims (prototype forbids them): `models/__init__.py:12-16` (`ServiceDeployment`/`DeploymentStatus` aliases), `deployment.py:15-25` (legacy enum), `queues.py:126-127` (`ensure_consumer_groups` alias), `__init__.py:3-7` (try/except → `RedisStreamClient = None`, which detonates later as a `NoneType` call).

---

## P2 — Missing abstractions / duplication

- **api task state machine** — the validate→set→event→commit→refresh ritual is inlined across 7 endpoints (`_task_actions.py:34-192` + `tasks.py:311`), and `spawn_worker` re-implements `start_task`'s promotion. Meanwhile `brainstorms.py:67-87` already solved it with one `_do_transition`. Extract one `_apply_transition(task, to_status, body, db, *, extra_details)`; ~120 lines → ~30.
- **api router scaffolding** — `_generate_id` copy-pasted in 4 routers; `_validate_transition` is three near-identical bodies differing only by enum, with a drifting error code (brainstorms raises 409, tasks/stories 422); `_get_<entity>` and `_resolve_user` duplicated. Move a generic `validate_transition(enum, map, frm, to)` and `get_or_404(db, Model, id)` to `shared/`.
- **api duplicate wire schemas** — `TaskCreate`/`TaskUpdate`/`TaskRead` in `schemas/task.py` duplicate `shared/contracts/dto/task.py` (`TaskDTO`) field-for-field; router uses the local copies, shared ones only used by tests. Delete local, import from shared.
- **scheduler retry/timeout** — counted four incompatible ways (two TTL'd Redis counters, a field smuggled through `run.result`, two task columns). Introduce one `RetryPolicy.incr_and_check(key, max)`. Timeout coverage is also ad-hoc per state (DEPLOYING/TESTING/PR_REVIEW have no watchdog, so a dead worker wedges a story forever); model stuck-detection as one `{status: threshold}` map.
- **langgraph two API clients** — architect uses typed `LanggraphAPIClient`; PO uses raw `httpx` with hardcoded `/api/...` paths and `.get()` defaults (`tools_shared.py:29`). Make PO use the same client. `LanggraphAPIClient` itself has duplicate methods (`create_service_deployment` vs `create_deployment`) and parallel public/private helper sets.
- **worker agent-type branching** — scattered `if agent_type == "claude"` across `wrapper.py` (session creation, prompt redirect, auto-resume, session-id scraping) plus a *separate* `worker-manager/agents/` abstraction for the same axis. Make the `Runner` the single agent-behavior abstraction.
- **langgraph deploy-success epilogue** — `deployer.py:308-333` and `:350-381` write the same finalize block twice (main vs rerun). Extract `_finalize_success(...)`.
- **telegram_bot dashboard URL** — the token-mint flow (`uuid4` → `redis.set(lk_token:...)` → build auth URL) is duplicated in `main.py:106-126` and `handlers.py:166-198`. One `create_dashboard_url(telegram_id)` helper. Security-sensitive, must stay in sync.
- **langgraph duplicated `_classification_to_outcome`** (`deploy_result_handler.py:58-62` vs `deploy_failure_handler.py:96-102`); **`_require_registry_env`** handled fail-fast in `deployer.py:92-103` but warn-and-continue in `_repo_setup.py:70-98`.

---

## P2 — File-size & decomposition

No file exceeds 1000 lines, but four are structurally overloaded:

- **`worker-wrapper/wrapper.py` (893)** — one class mixing six concerns. Lead with the self-contained ~190-line venv-path engine (`:281-472`, all static/pure): move to `venv_paths.py` as free functions. Then `task_archive.py` (report/archive/gitignore) and `workspace_prep.py` (file writers). Leaves a ~300-line consume/execute/publish orchestrator.
- **`scheduler/supervisor.py` (642)** — three unrelated sub-domains (story-architect retry, task failure/timeout, deploy+QA routing). Split into a `supervisor/` package; collapse `supervise_deploying_stories`/`supervise_testing_stories` (near-identical) into one `poll_runs_and_route(status, run_type, outcome_enum, handlers)`.
- **`telegram_bot/main.py` (531)** — command handlers + PO request/response plumbing + `ProactiveListener` + lifecycle wiring. Extract `po_client.py` and `listeners.py`; `main.py` shrinks to registration + lifecycle.
- **`langgraph/graph.py` `OrchestratorState`** — a 40-field god-TypedDict backing three flows for a graph that runs only the provisioner node, and `provisioner.py:114-138` injects ~9 keys not declared in the type. Split into per-flow TypedDicts and resync the declared keys.

---

## P2 — Non-atomic state / orchestration

- **`scheduler/supervisor.py:179-182`** — task retry is three independent API calls (transition BACKLOG → transition TODO → bump iteration). A crash between any two leaves a half-applied state, including TODO-without-iteration-bump, which re-dispatches and can loop forever. Add one server-side `tasks/{id}/retry` action.
- **`worker-manager/manager.py:128-187`** — `delete_worker` runs compose-down, container/network removal, lock release, and key deletion sequentially in one `try`; if `remove_container` raises, the `workspace:active_projects` lock leaks and `_check_project_lock` then rejects every future worker for that project. Release the lock in `finally`.
- **`worker-manager/manager.py:122-126` vs `:520-528`** — create-failure sets `worker:status=FAILED` then the outer except deletes the key (project_id path), so the spawner polling for FAILED sees UNKNOWN. Pick one source of truth.
- **`api`** — `complete_task` (`_task_actions.py:60-85`) fabricates three intermediate STATUS_CHANGE events (IN_DEV→IN_CI→TESTING→DONE) for transitions that never happened; the event log other services read becomes fiction. Either transition directly or tag synthetic events.

---

## Lower-priority nits

Tracked but low individual impact: scattered function-local imports in `api/routers/servers.py`/`applications.py` (re-importing `datetime`/models per handler); `scheduler` uses private `redis_client._redis` in two spots vs the public `.redis` property elsewhere; `user_id=""` placeholder on every scheduler-built message (a contract field never populated); `transition_story` hardcodes `actor="architect"` misattributing supervisor transitions; `INTERNAL_PROJECT_ID` magic UUID in `task_dispatcher.py:119-123` (wants a `project.internal` flag); `keyboards.servers_list_keyboard` ignores its only argument; N+1 fetches in `api/routers` done-sibling check, `tools_stories.get_story`, and `scheduler dispatch_todo_tasks`; duplicate `update_project` PUT/PATCH; `HTTP_OK = 200` redefined instead of `HTTPStatus.OK`; worker stream names defined twice (`queues.py` constants vs `WorkerChannels` enum) with `worker:lifecycle` absent from `QUEUE_TOPOLOGY`; `WorkerCommand`/`WorkerResponse` bare unions lacking a `Field(discriminator=...)`.

---

## Recommended sequencing (max leverage first)

1. **Security blockers first:** B1 (scaffolder exec), B2 (crypto), api fail-open auth, scaffolder token-in-URL. Small, high-risk, independent.
2. **Tighten `shared/` contracts (B7 + the duplicated vocabularies):** make response DTOs use their enums, add the `RunResult` union, collapse the agent/action/status vocabularies. This is the keystone. Most P1 typed-boundary rows downstream become mechanical (often deletions) once the contracts are right.
3. **Add `consume_typed` to the redis client**, then convert the worst raw-dict consumers (B6 worker result, engineering consumer, scheduler run.result).
4. **Delete the dead layers (P2):** tools/, worker:lifecycle, the second config cache, the compat shims. Pure subtraction, lowers the surface area for everything after.
5. **Fix the silent-failure sites (B3/B4 + the swallow list)** so the next bug is visible.
6. **Then the abstraction extractions and file splits**, which are now safer against a tightened, better-typed baseline.

Steps 2-4 are where the "dramatically simpler" payoff lives: tightening types at the source deletes defensive branches across seven services rather than rearranging them.
