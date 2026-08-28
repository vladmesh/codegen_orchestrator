# Agents and Nodes

Every agent is a LangGraph node with its own set of tools and its own specialization.

---

## 🧭 Product Owner (LangGraph ReactAgent)

**Role**: the central coordinator. Manages the project lifecycle through API tools, the single point of communication with the user.

**Implementation**: LangGraph `create_react_agent` in `services/langgraph/src/agents/po/`. Runs as an async consumer inside the langgraph container — no separate Docker container needed. Conversation state persisted via PostgreSQL checkpointer (`AsyncPostgresSaver`, schema `langgraph`); falls back to in-memory `MemorySaver` without `CHECKPOINT_DATABASE_URL`. Long conversations are compressed via `langmem.SummarizationNode` (`pre_model_hook`) — old messages are summarized into a running summary stored in `state["context"]` instead of being silently dropped.

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
the token is reusable: until `compose down -v` has run, the old bot is still long-polling and
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

**System events**: the PO consumer accepts three story-level events: `story_completed` (deploy success), `story_failed` (permanent failure after retries), `story_blocked` (developer hit a blocker, WAITING_HUMAN_REVIEW). All other system events are dropped — the PO checks progress through reminders.

**Communication**: Redis streams — `po:input` (inbound, user messages + system events), `po:response:{request_id}` (outbound, sync replies), `po:proactive` (outbound, async notifications). All PO streams use Pydantic contracts from `shared.contracts.queues.po` (`POInputMessage`, `POResponse`, `POProactiveMessage`) with flat-field serialization (`to_flat_fields()` / `from_flat_fields()`). PO Consumer has PEL recovery via `XAUTOCLAIM` on startup. Workers write system events to `po:input` via `callback_stream`. PO uses `notify_user` tool to send proactive messages when handling system events.

**Output**: actions through tools, messages to the user through Telegram

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

To resume: `POST /tasks/{id}/resume` (the admin gives guidance, task WHR → IN_DEV).

**Output**: code in the repository, pushed to the story branch | Or `GAVE_UP` → the WHR flow

---

## 🧪 Tester — removed

There is no Tester node. The Engineering Subgraph is `START → developer → done | blocked`.

Testing happens in two places instead. The Developer runs `make test` and `make lint` inside its own
worker before reporting a result, and CI runs on the pushed branch afterwards, where
`_wait_for_ci_and_fix` in `engineering_worker.py` handles a red gate. A future tester would sit
after deploy, validating a running service rather than a working tree.

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
2. **Environment observation** (`env-observation:queue`):
   - Reads one environment slot out of the containers a deployed service is running with
   - Changes nothing on the server, so repeating it is free
   - The answer is left in Redis under the request id, because the caller (the temporary-access
     sweep) is on a later tick by the time the playbook finishes

**Architecture**:
```
infra-service
  ├── Listen: provisioner:queue (RedisStreamClient.consume, auto_ack=False, claim_pending=True)
  ├── Listen: env-observation:queue (RedisStreamClient.consume_typed, manual ack)
  ├── Handlers:
  │   ├── process_provisioner_job() → ansible_runner.py
  │   └── observe_service_env() → ansible_runner.py (observe_service_env.yml)
  └── Publish: provisioner:results, env-observation results in Redis keys
```

**Output**: the results go to the Redis Stream `provisioner:results`

Server discovery is fail-closed: unknown provider servers are recorded as reserved and unmanaged.
The scheduler publishes provisioning triggers only for managed records, including its startup retry
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

**Important**: the PO ReactAgent coordinates the whole flow through LangChain tools. The Scaffolder (a separate service) prepares the repository (copier + make setup + git push) before the architect runs. Worker-manager mounts the pre-scaffolded workspace volume from `/data/workspaces/{repo_id}/` into the worker container. A deploy after a merge is detected by the PR poller in the scheduler; webhooks have been removed.
