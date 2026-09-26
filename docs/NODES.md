# Agents and Nodes

Every agent is a LangGraph node with its own set of tools and its own specialization.

---

## 🧭 Product Owner (LangGraph ReactAgent)

**Role**: the central coordinator. Manages the project lifecycle through API tools, the single point of communication with the user.

**Implementation**: LangGraph `create_react_agent` in `services/langgraph/src/agents/po/`. Runs as an async consumer inside the langgraph container. Conversation state is persisted via PostgreSQL checkpointer (`AsyncPostgresSaver`, schema `langgraph`); without `CHECKPOINT_DATABASE_URL` it uses in-memory `MemorySaver`. Long conversations are compressed via `langmem.SummarizationNode` (`pre_model_hook`) into a running summary in `state["context"]`.

**Model**: the PO and its summarizer each answer through their own [LLM channel chain](#-llm-channel-chain-architect-po-po-summarizer) (`po`, `po_summarizer`).

**Tools** (`src/agents/po/tools.py`):
- `create_project`, `list_projects`, `get_project`: project management through the API
- `set_project_secret`: storing secrets. Bot tokens are refused server-side (422) — the API only takes them through the validator.
- `validate_telegram_token`: posts the token to `POST /api/projects/{id}/telegram/token`. The API runs the check chain (format, `getMe`, then the external-activity probes: `getWebhookInfo` and a `getUpdates` probe that answers 409 when another poller holds the token, then uniqueness across
projects: a bot held by another live project is refused, naming that project only when the same
user owns it), stores `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` and `Repository.bot_username` only on a passing verdict, and returns a typed `TelegramTokenVerdict` (`shared/contracts/dto/telegram.py`) with a user-facing message. PO only relays it. The hold ends with the
project: archiving it, or its last application landing in `not_deployed` after an undeploy, clears
`Repository.bot_username` and drops the two secrets, so the bot is free to bind elsewhere
(`services/api/src/utils/telegram_binding.py`). Deleting the project removes the repository rows
outright, with the same effect.
- `teardown_project`: `POST /api/projects/{id}/teardown` then `GET` on the same path — owner-checked
teardown of the user's own project. The POST sends an undeploy (`DeployTrigger.PO`) to every
application still up and comes back `pending`; the GET reports where that stands and, once every
application reads `not_deployed`, archives the project and releases the bot. Only `completed` means
the token is reusable: until `compose down -v` has run, the bot is still long-polling and
Telegram answers 409 to whoever binds the token second, so the tool waits for that status before
telling the agent the bot is free. A failed undeploy run surfaces as `failed` rather than an endless
wait. Someone else's project is refused with 403 and stays untouched. This is the way out of a
`bound_to_own_project` verdict — PO offers the user the choice between continuing in the holding
project and freeing the token.
- `create_story`: creating a user story + automatically starting engineering work
- `reopen_story`: reopening a completed story with a user_report (the context of the problem)
- `list_stories`, `get_story`: viewing stories, the tasks attached to them and their runs (with id, status, type, error, timing)
- `get_run_status`: the detailed status of a specific engineering/deploy run
- `get_budget_balance`: the current user's exact known engineering spend and API-calculated
available amount. PO checks it before starting or reopening paid work and warns about low or
incomplete cost coverage without exposing reservation internals.
- `set_reminder`: deferred checks through a Redis ZSET
- `notify_user`: proactive message to user via `po:proactive` stream
- `web_search`: searching the documentation of external APIs through DuckDuckGo

**System events**: the PO consumer accepts three story-level events: `story_completed` (deploy success), `story_failed` (permanent failure after retries), `story_blocked` (developer hit a blocker, WAITING_HUMAN_REVIEW), and the non-terminal `story_requirements_returned` (the architect's admitted plan returned must-requirements; published by the architect consumer, replayed from the unacknowledged job until `po:input` accepts it), and the non-terminal `story_stage` (the scheduler's stage notice: the stage a story in work is at, what it waits for and the magnitude of the wait, on entry and after each quiet interval). The rest of `OwnerNotificationEvent` is routed too. All other system events are dropped — the PO checks progress through reminders.

**Communication**: Redis streams — `po:input` (inbound, user messages + system events), `po:response:{request_id}` (outbound, sync replies), `po:proactive` (outbound, async notifications). All PO streams use Pydantic contracts from `shared.contracts.queues.po` (`POInputMessage`, `POResponse`, `POProactiveMessage`) with flat-field serialization (`to_flat_fields()` / `from_flat_fields()`). PO Consumer has PEL recovery via `XAUTOCLAIM` on startup. Workers write system events to `po:input` via `callback_stream`. PO uses `notify_user` tool to send proactive messages when handling system events.

**Output**: actions through tools, messages to the user through Telegram

---

## 🔀 LLM channel chain (Architect, PO, PO summarizer)

The Architect, the PO and the PO summarizer do not hold one provider's model. Each gets one chat
model, `ChannelChainModel` (`services/langgraph/src/llm/`), built from an ordered **channel chain**
read from its `agent_configs` record (id `architect`, `po`, `po_summarizer`; field `llm_channels`).
`create_react_agent` and the PO `SummarizationNode` receive that model and nothing else.

**Channels**:
- `codex` — one model turn as one `codex exec` on the file-backed ChatGPT profile `LLM_CODEX_HOME`
  (`--ephemeral --ignore-user-config --skip-git-repo-check --sandbox read-only -c
  cli_auth_credentials_store="file"`), serialized by the profile's advisory lock
  `.codegen-codex.lock`, the same lock the worker wrapper holds. In the containers the profile is
  the Codex workers' own `HOST_CODEX_HOME`, and the CLI runs as its owner
  ([coding-agents.md](coding-agents.md#dedicated-chatgpt-session-profile)).
- `claude` — one model turn as one `claude -p --output-format json --json-schema … --tools ""
  --no-session-persistence` on `CLAUDE_CODE_OAUTH_TOKEN`, run as `nobody` when the service is root.
- `openrouter` — the existing `ChatOpenAI` on `ARCHITECT_LLM_*` / `PO_LLM_*` (the summarizer on
  `SUMMARIZATION_MODEL`, else `PO_LLM_MODEL`, over the PO's endpoint and key). `src/llm/openrouter.py`
  is the only module that builds `ChatOpenAI` or reads that env.

A CLI turn gets the serialized conversation (system, human, AI with tool calls, tool results) and the
bound tools' names, descriptions and argument schemas on **stdin**, never argv, and is held to the
output schema `{content, tool_calls: [{name, arguments_json}]}`. Arguments are parsed and validated
against the bound tool's schema and become `tool_calls` with generated ids. The process runs in an
empty temporary directory with no tools of its own and an environment of PATH, locale, its own
credential variable and a HOME of its own that is deleted with the call — never an API key, Redis
or database URL of langgraph. The langgraph image installs both CLIs at the worker images' pins and
its build fails if an installed version differs from the pin or lacks a flag the adapter passes
(`src/llm/cli_contract.py`).

**Configuration**: `llm_channels` is a list of `{channel, model?, timeout_seconds?}`
(`shared/contracts/dto/llm_channel.py`); the API refuses an empty list, an unknown channel or a
duplicate. No record or `null` means the default chain `codex, claude, openrouter`. `model` unset
means the CLI's own default model, or the agent's env model for `openrouter`; `timeout_seconds`
unset means 600 s for one model turn, except the `codex` and `claude` channels of the PO and the PO
summarizer: a user waits on those turns, so a subscription CLI gets 180 s before the turn moves on. A stored chain that does not validate stops the consumer at
startup with `invalid_llm_channel_chain`. An agent is refused (Architect) or disabled (PO) only
when no channel of its chain is configured — no `LLM_CODEX_HOME`, no `CLAUDE_CODE_OAUTH_TOKEN`, no
complete OpenRouter env for the channels it names; otherwise a missing credential is that
channel's failure and the chain moves on.

**Switching**: a call tries the channels in order and returns the first answer. These failure
classes move the same call to the next channel: `unauthorized` (401), `payment_required` (402),
`forbidden` (403), `rate_limited` (429), `server_error` (5xx), `unreachable` (no connection),
`quota_exhausted` (usage limit or credits), `timeout` (the channel's deadline, lock wait included),
`missing_credential`, `binary_missing`, `nonzero_exit` and `invalid_output` (not schema-valid, an
unbound tool, or arguments failing the tool's schema, after one corrective re-ask on the same
channel). Anything else — a bug, a provider 400, cancellation — propagates without switching. When
every channel failed, `LLMChannelsExhausted` names each channel and its failure class.

**Readiness**: at startup each agent logs `llm_channel_ready` per channel of its chain — `agent`,
`channel`, `status` (`ready` or the failure class a call would hit first), `reason`,
`cli_version` — without calling a model or running a CLI against a credential.

**Recording**: `llm_channel_used` and `llm_channel_failed` per call (see
[LOGGING.md](LOGGING.md#langgraph-worker)); the answering channel is in the returned message's
`response_metadata["llm_channel"]`; the Architect's `architect_job_success` / `architect_job_failed`
name the channels its planning attempt used and the failures it skipped.

**Operator alerts** (`src/llm/alerts.py`): the chain reports every failure and answer to one alert
module, which sends to administrators in a bounded background task and never touches the call.
A `payment_required` (402) on any channel of any agent alerts once per channel; one call that
failed both `codex` and `claude` and was answered by `openrouter` alerts "subscription channels
down, <agent> running on OpenRouter" once per agent. The langgraph process also reads the
OpenRouter balance (`GET <PO_LLM_BASE_URL>/credits`, `total_credits - total_usage`) every
`llm.openrouter_balance_check_interval_minutes` and alerts below `llm.openrouter_balance_alert_usd`,
re-armed once the balance is back above it; a 401/402/403 on that read is alerted like a refused
channel, and without an OpenRouter key the check logs `openrouter_balance_check_idle` and stops.
Dedup is a Redis key `llm:alert:<kind>:<channel or agent>` shared by langgraph and architect,
set only after an administrator accepted the alert, with `llm.alert_realert_window_hours` as TTL.
A missing or unreadable config key falls back to its documented default with a warning.

**Degraded mode**: when the PO's call reaches `openrouter` after `codex` and `claude` both failed
it, the chain appends `PO_SUBSCRIPTIONS_DOWN_NOTE` (`src/llm/agent.py`) as a system message to
that call only: answer normally, keep collecting requirements, tell the user once that
engineering capacity is temporarily unavailable, promise no timeline. The PO summarizer and the
Architect get no note; no note is added when a subscription channel answered.

---


## 👨‍💻 Developer (Engineering Subgraph)

**Role**: writing the business logic in an already scaffolded project.

**When it is called**:
- The only working stage of the Engineering Subgraph
- On a red CI gate, through `_wait_for_ci_and_fix` in `engineering_worker.py`

**Implementation**:
1. The Scaffolder service (a separate microservice) runs the scaffold phase: copier + make setup + git push, saves the tree + specs_summary to the DB, sets `project.status = active`
2. The Architect Consumer (langgraph) waits for the scaffold to finish (polling project.status != draft, up to 5 min), then decomposes the story into tasks (it sees the tree and the specs summary: models, domains, events)
3. The Task Dispatcher finds unblocked tasks, creates Runs, publishes to `engineering:queue` with `branch=story/{story_id}`
4. The engineering worker creates the GitHub repository and sets the registry secrets
5. Spawns a container through `worker-manager` (Claude Code / Factory.ai / OpenAI Codex)
6. Worker-manager creates/checks out the `story/{story_id}` branch and injects the instructions from `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` and `TASK.md` (into `/workspace/TASK.md`)
7. The agent works on the feature branch and pushes to it

**Validation**: checks that a commit SHA is present in the result.

**Handling gave-up**: if the developer agent cannot complete the task (missing credentials, 404 URLs, contradictory requirements), it calls `curl -X POST localhost:9090/result -d '{"success":false,"reason":"..."}'`. The worker-wrapper HTTP server accepts the request and publishes the result to Redis. The Developer node returns `engineering_status=EngineeringStatus.GAVE_UP`. The engineering consumer calls `handle_worker_gave_up()`:
- Task → `waiting_human_review` with `failure_metadata = {reason: "..."}`
- Story → `waiting_human_review`
- The admin is notified through `notify_admins()` (level=warning)
- The user is notified through the PO (a `story_blocked` event)
- The worker container is **not removed** (the admin can inspect it)

To resume: `POST /tasks/{id}/resume`, the one operator retry — the admin gives guidance, the task goes WHR → `todo` on a fresh iteration with its own retry budget, the story returns to `in_progress`, and the dispatcher starts a new run (see PIPELINE_V2 → Operator resume).

**Output**: code in the repository, pushed to the story branch | Or `GAVE_UP` → the WHR flow

---

## 🔧 DevOps (Subgraph)

**Role**: deployment with a typed environment contract.

**When it is called**:
- After the Engineering Subgraph
- On `trigger_deploy` from the PO
- When a merged PR is detected (the PR poller in the scheduler, 30s poll) → deploy:queue

**Package structure** (`src/subgraphs/devops/`):
```
devops/
├── __init__.py          # Exports
├── state.py             # DevOpsState TypedDict
├── env_contract_loader.py # Loading and validating the mandatory contract
├── nodes.py             # SecretResolver, ReadinessCheck, Deployer, SmokeTester
└── graph.py             # Routing + create_devops_subgraph
```

**Nodes inside the subgraph**:

1. **EnvironmentContractLoader**: loads the `env.contract.yaml` fragments from the
   repository. A missing or invalid contract ends the deploy with a
   distinguishable contract outcome.

2. **SecretResolver (Functional)**:
   - Decrypts the existing secrets from the DB (`decrypt_dict`)
   - Resolves the production values of the mandatory typed contract: user secrets, generated secrets, allocations, derived and literal values
   - Stores the generated secrets, checks that the required user secrets are present
   - Encrypts the new secrets and saves them back to the DB (`encrypt_dict`)

3. **ReadinessCheck (Functional)**:
   - Checks readiness for deployment
   - If there are missing_user_secrets → back to the PO
   - If everything is ready → the Deployer

4. **Deployer (Functional)**:
   - Builds DOTENV from `secret_values` and `non_secret_values` (`build_dotenv` → `encode_dotenv` → base64)
   - Writes 9 GitHub Secrets: DOTENV, DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PORT, PROJECT_NAME, REGISTRY_URL, REGISTRY_USER, REGISTRY_PASSWORD
   - Triggers `deploy.yml` through `trigger_workflow_dispatch`
   - Waits for completion through `wait_for_workflow_completion` (poll, timeout 600s)
   - Post-deployment operations:
     * Creates or updates Application record (repo + server → runtime entity with `ApplicationStatus`)
     * Creates a Deployment record (an immutable deploy log with `DeploymentResult` and `deployed_sha`)
     * Sets the project status = active

5. **SmokeTester (Functional)**:
   - Does an HTTP `/health` check for backends; for tg_bot it is the Bot API `getMe` with the project's token
     plus `docker compose ps` on the server to confirm that the `tg_bot` container is running. Both probes are mandatory:
     a missing token, a missing server handle or missing SSH is a `fail` with the reason text, not a skip.
   - Implements retry logic (3 attempts, 5s delay).
   - On failure: SSHes into deploy server, captures `docker compose logs --tail=50`, appends to check `detail` field. Logs flow through deploy→engineering feedback loop so fix tasks get actual tracebacks.
   - Writes `smoke_result` into `DevOpsState` to pass the status through to the deploy worker.

**Architecture**:
```
Deployer → build_dotenv → set_repository_secrets (GitHub API)
                        → trigger_workflow_dispatch (deploy.yml)
                        → wait_for_workflow_completion (poll)
                                       ↓
                              GitHub Actions Runner
                                       ↓
                              Docker build + deploy to VPS
```

**Output**:
- `deployed_url` on success
- `missing_user_secrets` if secrets are needed from the user

**Proactive notifications**:
Filtered to reduce spam — only two events reach user via `po:proactive`: (1) deploy success (deployed URL), (2) permanent story failure (user-friendly message). All intermediate failures (smoke, precheck, workflow) are routed through the deploy→engineering feedback loop for automated fixing.

**Deploy→Engineering Feedback Loop**:
Deploy worker writes `DeployOutcome` to `run.result`. The supervisor (`supervise_deploying_stories()` in scheduler) reads this and routes: `CODE_FIX` → creates fix task and dispatches to `engineering:queue`, `RETRY` → redeploys (max 3), `GIVE_UP` → story fails and admin is notified. Deploy worker no longer transitions stories or creates tasks directly.

---

## 🚧 Infra Service

**Role**: an isolated service for running Ansible operations (provisioning).

**Implementation**: a separate `infra-service` service to isolate the heavy dependencies (Ansible, SSH).

**Job types**:
1. **Provisioning** (`provisioner:queue`):
   - Rejects servers whose database record is not explicitly managed
   - A password reset through the Time4VPS API
   - An OS reinstall only after an explicit force-rebuild request and only when the provider ID is
     present in `PROVISIONING_POLICY_TIME4VPS_MANAGED_SERVER_IDS`; SSH failure alone is non-destructive
   - Ansible playbooks for server setup
   - Redeploying the services after recovery
**Architecture**:
```
infra-service
  ├── Listen: provisioner:queue (RedisStreamClient.consume, auto_ack=False, claim_pending=True)
  ├── Handlers:
  │   └── process_provisioner_job() → ansible_runner.py
  └── Publish: provisioner:results
```

**Output**: the results go to the Redis Stream `provisioner:results`

Server discovery is fail-closed: unknown provider servers are recorded as reserved and unmanaged.
`scheduler-infrastructure` publishes provisioning triggers only for managed records, including its startup retry
path. The infra-service repeats both the managed-record check and the provider-ID allowlist check
before any provisioning path, then repeats the allowlist at the destructive operation boundary, so
direct or stale queue messages cannot bypass the discovery policy. Unauthorized scheduled rows are
neutralized to `reserved` with an admin alert. For an authorized `force-rebuild`, the scheduler keeps
that explicit status until infra-service reads it and changes the status to `provisioning` before
entering the guarded reinstall path.

---

## 🔄 Interaction

```
User (Telegram)
     │
     ▼
Telegram Bot → Redis (po:input)
     │
     ▼
PO ReactAgent (in langgraph container)
     │ tool calls (httpx/Redis)
     ├──────────────▶ po:response:{request_id} ──▶ User
     │
     ├──────────────▶ scaffold:queue → Scaffolder Service
     │               (copier + make setup + git push, saves tree + specs_summary)
     │                     │
     │                     ▼
     │               architect:queue → Architect Consumer
     │               (waits for scaffold, then LLM: story → tasks with specs context)
     │                     │
     │                     ▼
     │               Task Dispatcher → engineering:queue
     │                     │
     │                     ▼
     │               Engineering Subgraph
     │               eng-worker: create repo + secrets
     │                     │
     │                     ▼
     │               Developer node → worker-manager
     │               → agent writes code → CI gate
     │                                     │
     ├──────────────▶ trigger_deploy ◄─────┘
     │                     │
     │                     ▼
     │               DevOps Subgraph
     │               EnvironmentContractLoader → SecretResolver → ReadinessCheck → Deployer
     │                                                      │
     └──────────────▶ (completion) ◄─────────────────────────┘


PR Poller (scheduler, 30s poll)
     │ detects merged PR on story/* branch
     ▼
Scheduler → story → deploying, create Run
     │
     ▼
Redis (deploy:queue) → deploy-worker → DevOps Subgraph
     │
     ▼
Redis (po:proactive) → Telegram Bot → User
```

**Important**: the PO ReactAgent coordinates the flow through LangChain tools. The Scaffolder prepares the repository before the architect runs. Worker-manager mounts the pre-scaffolded workspace volume from `/data/workspaces/{repo_id}/` into the worker container. The `scheduler-pipeline` PR poller detects a merge and triggers deploy.
