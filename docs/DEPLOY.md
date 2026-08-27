# Production Deployment

## Server Prerequisites

- Ubuntu 22.04+ with Docker Engine 24+ and Docker Compose v2.24+
- `deploy` user with `docker` group membership and sudo access
- Directories: `/opt/codegen_orchestrator` (git clone), `/opt/secrets`, `/opt/backups/orchestrator`
- Git clone: `git clone <repo> /opt/codegen_orchestrator`
- Ports 80/443 open (Caddy handles TLS)

## Admin access over SSH

The admin frontend has no public Caddy route. Production Compose binds it only to
the server loopback interface, and nginx Basic Auth protects the SPA, `/api/`,
`/wm-api/`, and `/grafana/`. Connect from an operator workstation with:

```bash
ssh -N -L 3001:127.0.0.1:3001 deploy@PROD_HOST
```

Open [http://127.0.0.1:3001](http://127.0.0.1:3001) locally and authenticate with
the required `ADMIN_USER` and `ADMIN_PASSWORD` from the production environment.

Before handing access to an operator, check the listener on the server:

```bash
sudo ss -ltn '( sport = :3001 )'
```

It must show `127.0.0.1:3001`, never `0.0.0.0:3001`, `[::]:3001`, or a public
interface. While the tunnel is running, an unauthenticated request must return
401 and a credentialed request must succeed:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3001/
curl -fsSI -u "$ADMIN_USER:$ADMIN_PASSWORD" http://127.0.0.1:3001/
```

Confirm Caddy has no admin route before deployment:

```bash
rg -n 'admin-frontend|handle /api|handle /wm-api|handle /grafana' infra/Caddyfile
```

The command must print no matches. Do not add a Caddy route for these surfaces.

## Claude and Codex executor diagnostics

Worker-manager publishes a complete Claude/Codex diagnostic snapshot to Redis at
startup and every 30 seconds. Its 90-second TTL is intentional: a missing,
stale or malformed snapshot is `unknown` in the admin Settings surface and
blocks a new paid run until an authenticated admin explicitly confirms the
current snapshot version. The confirmation is valid for multiple starts only
until that snapshot expires; refreshing diagnostics invalidates it.

The worker-manager only performs local checks. It does not refresh tokens,
contact a provider, check quota or make a billable model request. `available`
therefore means the configured local session and Docker/Redis inventory
reconciled, not that a provider account has capacity. `unavailable` means a
local configuration/authentication failure; `unknown` means the service cannot
prove the state. The Settings card never displays paths or credential detail.
The currently deployed paid engineering and QA producers use `host_session`;
their diagnostics validate the manager-visible read-only mounts
`/host-claude` and `/host-codex`, while `HOST_CLAUDE_DIR` and
`HOST_CODEX_HOME` remain the Docker-host source paths used for worker mounts.
An unreconciled Docker/Redis inventory displays active leases as unknown, never
as zero. Reconciliation compares both directions by worker id, ownership,
executor and auth-mode labels, plus Redis/Docker terminal state. A missing
counterpart, unreadable or unknown status, label mismatch, or lifecycle mismatch
is unknown. A disabled executor remains unavailable but keeps a reconciled live
lease count.

For local recovery, use `claude auth login` to repair the dedicated
`HOST_CLAUDE_DIR` profile, or `codex login --device-auth` to repair the dedicated
`HOST_CODEX_HOME` profile. Then verify permissions and structure with
`./scripts/stand_preflight.py --no-probe`; this uses the same local validators
as worker creation and performs no provider probe. Do not point either setting
at an operator's ordinary home profile.

## Managed project deploy target

The provisioner prepares `/opt/services` for the `Server.ssh_user` configured on
the target. It creates that user, adds the orchestrator SSH key and Docker group
membership, then sets `/opt/services` to `root:<ssh_user>` with mode `3770`.

The group write bit lets the deploy workflow create `/opt/services/<project>` on
its first `create` deploy. The sticky bit prevents that user from renaming or
removing a root-owned project root. Existing projects remain `root:root 0755`
(or stricter), so the deploy user cannot write `personal_site` or any other
existing root-owned project directory. New project roots belong to the deploy
user and can be updated by their own feature and fix deploys.

The runtime precheck intentionally does not create directories: `create` must
observe an absent project directory, while `feature` and `fix` require one. The
generated workflow creates the directory only after the `create` precheck has
passed.

The adopted target `vps-273978` received the monitoring baseline on 2026-07-27; see the
section below for the operation. The `/opt/services` ownership contract is applied by the
`deploy_target` role, which the baseline run does not cover — it applies only the
`monitoring` tag.

### Destructive provisioning safety

Time4VPS discovery is default-deny. `TIME4VPS_MANAGED_SERVER_IDS` must contain the comma-separated
provider IDs of the servers this installation is allowed to provision. An absent or empty value
means that no Time4VPS server is managed. Every other newly discovered server is stored as
`is_managed=false` with status `reserved`; it is visible to inventory reads but never enters a
provisioning trigger.

The same allowlist is checked again by `infra-service` before either Ansible or reinstall work and
once more immediately before an OS reinstall. The `is_managed` database flag and the scheduler
trigger filters are separate guards, so a stale status or a manually published queue message cannot
by itself authorize provisioning.

To adopt a new blank target:

1. Read its immutable provider ID from the Time4VPS account and verify the target by both ID and IP.
2. Set the production GitHub secrets `TIME4VPS_MANAGED_SERVER_IDS`, `TIME4VPS_LOGIN`,
   `TIME4VPS_PASSWORD`, and `ORCHESTRATOR_PUBLIC_IP`, then run the deploy workflow. The workflow owns
   and rewrites the server `.env`; all four values are preserved from those secrets.
3. A brand-new allowlisted provider server enters `pending_setup`. Adding an existing inventory row
   to the allowlist only marks it managed and sends an alert; it does not schedule work. Restoring
   an accidentally removed ID preserves the server's prior operational status. For a verified blank
   existing row, explicitly PATCH its status to `pending_setup` to use the non-destructive SSH path.
4. If a verified blank server has no working orchestrator SSH access, request `force-rebuild`
   explicitly through the admin API and watch the provisioning logs. The scheduler keeps that
   persisted intent until infra-service claims it, then infra-service changes the lifecycle status
   to `provisioning` immediately before the guarded reinstall path.

The allowlist authorizes both non-destructive Ansible maintenance and an explicit admin-requested
reinstall; it does not schedule or reinstall an existing server merely because its ID was added.
Never list personal or development servers. A populated production server may be listed for adopted
maintenance only after its identity and backups are verified; protect admin/internal API credentials
because an explicit `force-rebuild` request is the remaining reinstall authority. A failed SSH probe
alone never authorizes reinstall.

## Monitoring baseline for adopted servers

To install monitoring without reinstalling or running the full provisioning path,
first ensure the adopted server is managed and its provider ID is in
`TIME4VPS_MANAGED_SERVER_IDS`, then run the supported operation from the infra-service container:

```bash
docker compose exec infra-service python -m src.provisioner.monitoring_baseline SERVER_HANDLE
```

It runs only the `monitoring` tag in `provision_software.yml`, then checks the
target node exporter from the Ansible controller. A failed HTTP check makes the command
fail and does not record the baseline as applied. After a successful run,
`GET /api/servers/SERVER_HANDLE/monitoring-status` shows the baseline timestamp,
the last successful exporter observation and the newest metrics sample. Its
`not_provisioned` state is distinct from a baseline that is waiting for metrics.

`server_unreachable` incidents are sent to administrators by the health checker
when the exporter cannot be fetched, and resolved notifications are sent when it
returns. They also remain visible through `GET /api/servers/SERVER_HANDLE/incidents`.

### Known issue: promtail crash-loops after a baseline run

The `monitoring` role writes a compose file that mounts `/opt/monitoring/promtail.yml`
unconditionally, but renders that file only `when: loki_push_url is defined`. The variable
lives in `ansible/group_vars/all.yml`, which is never loaded: the runner writes its inventory
to a temporary file elsewhere, and `provision_software.yml` loads only `provision_vars.yml`
explicitly. With the variable undefined the template step is skipped, Docker then creates
`/opt/monitoring/promtail.yml` as a *directory*, and promtail restarts forever with
`Unable to parse config: ... is a directory`.

Until the role is fixed, check promtail after a baseline run. `node_exporter` and `cadvisor`
are unaffected, so server metrics keep flowing. On `vps-273978` promtail was stopped and the
stray directory removed on 2026-07-27; the host has no orchestrator-deployed containers to
scrape, so nothing is lost.

## GitHub Secrets

All secrets must be configured in the repository's **production** environment.

### SSH & Server

| Secret | Description |
|--------|-------------|
| `PROD_HOST` | Server IP or hostname |
| `SSH_PRIVATE_KEY` | Deploy key for SSH to prod server (GitHub Actions → prod) |

> **Note:** SSH keys for managed servers are stored per-server in the database
> (encrypted with Fernet). The infra-service generates a key pair during provisioning
> and saves it via the API. No SSH key mounting is needed in docker-compose.

### Database

| Secret | Description |
|--------|-------------|
| `POSTGRES_DB` | Database name |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |
| `GRAFANA_DB_USER` | Dedicated read-only PostgreSQL role used by Grafana |
| `GRAFANA_DB_PASSWORD` | Password for the Grafana read-only PostgreSQL role |

### Internal Service Authentication

| Secret | Description |
|--------|-------------|
| `INTERNAL_API_KEY` | Random shared credential sent by trusted services as `X-Internal-Key` |

The key says which caller this is, not whose behalf it acts on. A request that also
carries `X-Telegram-ID` — the PO agent's and the bot's do — is judged as that user, so
holding the key does not open a stranger's project, run or admin endpoint. A service call
that names no user is unrestricted, as before. `resolve_actor` in
`services/api/src/dependencies.py` is where that is decided; every access guard asks it.

Without the key — or an LK bearer token — nothing reaches a handler at all: every route
except `GET /`, `GET /health` and `POST /api/lk/auth/token` is closed by
`require_authenticated_caller`. So the key is required by anything that talks to the API,
including `make seed`, the scripts under `infra/scripts/` and the admin frontend's nginx
proxy, which stamps it into the requests it forwards. `X-Telegram-ID` alone is refused, and
`is_admin` on `POST /api/users` is accepted only from an internal caller — a container that
can reach the API's port can no longer write itself an administrator.

### Worker broker authentication

| Secret | Description |
|--------|-------------|
| `WORKER_BROKER_INTERNAL_TOKEN` | Non-empty shared manager-to-broker credential. Generate with `openssl rand -hex 32`. |

The deploy workflow writes this value to `.env` before Compose starts. Both
`worker-manager` and `worker-broker` reject a missing or empty value at boot; it
is never passed into coding-worker containers.

### LLM Providers

| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPEN_ROUTER_KEY` | OpenRouter API key |
| `PO_LLM_MODEL` | PO agent model name |
| `PO_LLM_BASE_URL` | PO agent LLM base URL |
| `PO_LLM_API_KEY` | PO agent LLM API key |
| `ARCHITECT_LLM_MODEL` | Architect agent model name |
| `ARCHITECT_LLM_BASE_URL` | Architect agent LLM base URL |
| `ARCHITECT_LLM_API_KEY` | Architect agent LLM API key |
| `SUMMARIZATION_MODEL` | Summarization model name |
| `SUMMARIZATION_MAX_TOKENS` | Max tokens for summarization |
| `SUMMARIZATION_TRIGGER_TOKENS` | Token threshold to trigger summarization |
| `SUMMARIZATION_MAX_SUMMARY_TOKENS` | Max summary output tokens |

The `PO_LLM_*` and `ARCHITECT_LLM_*` triples are all-or-nothing: an agent starts only when
every var of its group carries a value, so leaving one of the three empty silently keeps that
agent out of the pipeline. `services/langgraph/src/config/agent_llm_env.py` is the single source
of truth for the groups.

### GitHub Integration

| Secret | Description |
|--------|-------------|
| `GH_APP_ID` | GitHub App ID |
| `GH_APP_PRIVATE_KEY` | GitHub App private key PEM (written to `/opt/secrets/github_app.pem`) |
| `GH_ORG` | GitHub organization name |
| `GHCR_TOKEN` | GitHub token with `packages:read` scope (for pulling worker images) |

`GH_ORG` is the name of the *secret* only. The rename stopped at the secret: the deploy workflow
writes it into the server `.env` as `GITHUB_ORG=${{ secrets.GH_ORG }}`
(`.github/workflows/deploy.yml`), and that is still the env var every service reads. So a secret
named `GITHUB_ORG` is read by nothing — the workflow's required-secret preflight fails the deploy
before it starts when `GH_ORG` is empty.

**How the GitHub App key reaches a service.** Three names carry one key, and they are not
interchangeable:

- `GH_APP_PRIVATE_KEY` — the GitHub secret, holding the PEM itself.
- `GITHUB_APP_PEM_PATH=/opt/secrets/github_app.pem` — the host path the deploy writes that PEM to
  (mode 0600).
- `GITHUB_APP_PRIVATE_KEY_PATH=/app/keys/github_app.pem` — the in-container path the service
  reads.

Compose bind-mounts the host path onto the container path read-only
(`${GITHUB_APP_PEM_PATH:-./secrets/github_app.pem}:/app/keys/github_app.pem:ro`). Neither path is a
GitHub secret: the deploy workflow writes both into the server `.env` itself, so do not configure
them in the repository environment. Services fail fast when the in-container path holds no key.

### Telegram

| Secret | Description |
|--------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `ADMIN_TELEGRAM_IDS` | Comma-separated admin Telegram IDs |
| `TELEGRAM_ID_ADMIN` | Primary admin Telegram ID (for seeding) |
| `TELETHON_API_ID` | Telegram API ID for the QA runtime's Telethon client |
| `TELETHON_API_HASH` | Telegram API hash for the QA runtime's Telethon client |
| `TELETHON_SESSION` | Authorized Telethon session string for the QA account |

All three `TELETHON_*` secrets are required to test Telegram bots: `qa-worker` reads them from its
own environment and talks to the bot as the QA account from there. They are never written to a
deploy target. Without them a bot story is blocked with `missing_telethon_credentials` instead of
being tested. See [QA runtime](#qa-runtime-central) below.

### User Dashboard (LK)

| Secret | Description |
|--------|-------------|
| `LK_DOMAIN` | Public URL of the user dashboard, used by the bot to build dashboard links |
| `LK_JWT_SECRET` | Key the API signs and verifies dashboard access tokens with |

Both reject an empty value at service startup — an unset one arrives through compose as `""` and
would otherwise sign dashboard tokens with a known key.

### Encryption & Registry

| Secret | Description |
|--------|-------------|
| `SECRETS_ENCRYPTION_KEY` | Fernet key for encrypting project secrets |
| `ORCHESTRATOR_HOSTNAME` | Public hostname (for Caddy TLS, registry) |
| `ORCHESTRATOR_PUBLIC_IP` | Public egress IP allowed to reach node monitoring ports |
| `TIME4VPS_MANAGED_SERVER_IDS` | Required non-empty production allowlist of provider IDs the orchestrator may provision or reinstall; empty is supported only as a default-deny local mode |
| `TIME4VPS_LOGIN` | Time4VPS login used by infra-service provider verification |
| `TIME4VPS_PASSWORD` | Time4VPS password used by infra-service provider verification |
| `REGISTRY_USER` | Docker registry basic auth user |
| `REGISTRY_PASSWORD` | Docker registry password |
| `REGISTRY_PASSWORD_HASH` | Bcrypt hash of registry password (for Caddy) |

### Worker Agents

| Secret | Description |
|--------|-------------|
| `FACTORY_API_KEY` | Factory.ai API key |
| `HOST_CLAUDE_DIR` | Path to `.claude` directory on prod server |
| `HOST_CODEX_HOME` | Path to the dedicated file-backed Codex profile described in `docs/coding-agents.md` |

### Admin UI

| Secret | Description |
|--------|-------------|
| `ADMIN_USER` | Admin panel basic auth username |
| `ADMIN_PASSWORD` | Admin panel basic auth password |

Both values are mandatory. The admin frontend fails to start if either is empty.

### Observability

| Secret | Description |
|--------|-------------|
| `LANGCHAIN_API_KEY` | LangSmith API key (optional, for tracing) |
| `LOKI_PUSH_USER` | Basic-auth user for remote Promtail writes |
| `LOKI_PUSH_PASSWORD` | Plaintext password configured on managed Promtail clients |
| `LOKI_PUSH_PASSWORD_HASH` | Caddy-compatible bcrypt hash of the Loki push password |
| `GRAFANA_ADMIN_PASSWORD` | Grafana administrator password |

## QA runtime (central)

Exploratory QA is performed on the management host by an ephemeral coding agent that
`qa-worker` starts through worker-manager, on the same subscription session developer workers use.
Deploy targets carry nothing for it: no CLI, no LLM credentials, no Telethon session.

**What the QA runtime needs** (all in the orchestrator `.env`):
- `QA_EXECUTOR_AGENT_TYPE` — who performs the run. `codex` by default; `claude` remains an explicit
  subscription-agent override, and nothing else: `factory` (provider API key) and `noop` (no testing at all)
  are refused when the configuration is read, and a `qa` worker command carrying either is refused
  by worker-manager before a container exists. The session itself is `HOST_CLAUDE_DIR` /
  `HOST_CODEX_HOME`, which worker-manager mounts into the ephemeral QA container.
- `QA_CAPABILITY_HOST` — how that container addresses `qa-worker`'s per-run capability endpoint.
  It is the service's name on the `codegen_worker` network and only changes if the service is
  renamed. `qa-worker` is attached to that network for this and for nothing else. Codex QA invokes
  its documented `--skip-git-repo-check` mode because its ephemeral workspace intentionally has no Git repository.
- `TELETHON_API_ID`, `TELETHON_API_HASH`, `TELETHON_SESSION` — the QA Telegram account, needed only
  for projects with a bot.
- `QA_LLM_MODEL`, `QA_LLM_BASE_URL`, `QA_LLM_API_KEY` — **optional**. An API fallback consulted only
  after the assigned executor has actually failed to run (no session, expired session, broken CLI,
  container never started). Leaving all three empty is a supported production configuration. If the
  executor fails and there is no complete triplet, the run ends as `qa_executor_unavailable` — a
  QA-infrastructure outcome that alerts administrators and sends the story to human review, never a
  product defect. Health-only criteria run with no executor at all.

**What the QA container can reach.** It has a shell, and that shell reaches nothing of the platform:
no SSH key, no fleet key, no Telegram session, no provider key, no repository. Its whole route to
the deployment is one injected command (`/workspace/qa`) that posts named calls to the per-run
capability endpoint, which performs them from `qa-worker` with the run's borrowed `qa-observer`
identity. The endpoint accepts GET-only HTTP calls, reads inside the deployment's physical root,
and read-only docker sub-commands against the deployment's own containers — the same closed set as
before.

That the container *cannot* go around this is a property of its network, not of the prompt. The QA
executor is attached to `codegen_qa_egress` and to nothing else, and that network is declared
`internal: true`: it has no route to the deployment's public URL, to the fleet, or to the internet.
Reachable on it are the run's capability endpoint (`qa-worker`), the worker broker — the runtime's
own control channel — and one per-run egress proxy. That proxy speaks `CONNECT` only, to the
assigned CLI's model backend and nothing else (`QA_CLAUDE_BACKEND_HOSTS` /
`QA_CODEX_BACKEND_HOSTS`), so it can carry the model traffic the CLI needs and cannot carry a
request to the application. `worker-manager` proves the network is internal before it creates
anything, proves the proxy is listening before the executor exists, and proves the started
container is attached to that single network — any of those failing fails the run closed as a
QA-infrastructure outcome rather than starting an unrestricted container. Proxy variables are set
in the executor's environment for the CLI's convenience; stripping them reaches less, not more.

The runner's write scan over the tool trace and the container's transcript is still there, and it
still fails the run closed with a residual-state record. It is now a second layer over an enforced
boundary rather than the boundary itself. `services/worker-manager/tests/service/test_qa_egress_boundary.py`
proves it against a real daemon: a recording application, a real executor container, `POST`/`PUT`/
`PATCH`/`DELETE` from `curl` and from Python with the proxy configuration stripped, and zero write
requests in the application's own ledger.

**Which identity a run uses.** Not `servers.ssh_user`: that column is the administrative account the
fleet key opens (`root` on every row `server_sync` creates), and a run holding it would have the
platform's own authority over the deployment it is testing. The run uses `qa-observer`, an account
**provisioning** creates — the `qa_identity` role, included by `provision_software.yml`, which is the
phase that writes `labels.provisioning_phase=complete`. The same completion write records
`labels.qa_ssh_user`, and that label is what the QA runtime reads. A host recorded complete by the
current provisioner therefore always has the account; a host that has neither ran an older one.

The label answers *whether* this host was provisioned by an Ansible that creates the account — not
*as whom*. `servers.labels` is an untyped dict that `PATCH /api/servers/{handle}` will write, so the
runtime accepts only the one name provisioning writes (`qa-observer`); a label naming anything else
is refused exactly like a missing one, with reason `qa_identity_not_attested`. Editing a server row
is therefore not a way to point a QA run at some other existing account. One consequence worth
knowing: renaming the account is fail-closed — hosts still carrying the old name lend nothing until
the retrofit has run over them.

**The account has to be provisioning's own, and that is checked on the target.** A host can already
carry a local account called `qa-observer` that nobody here created, and the role's tasks would not
take away what such an account might have — `uid 0`, a rule in somebody else's file under
`/etc/sudoers.d`, an ACL straight on the docker socket. So the role establishes two things before
anything records that this host has an identity:

- **ownership.** `/etc/codegen-qa-identity/qa-observer` is a root-owned file the role writes when it
  creates the account. An account of that name found *without* it was created by somebody else, and
  the role fails there: it changes nothing, deletes nobody's sudoers file, and the host is left with
  no QA identity. Rename or remove that account by hand and run provisioning again.
- **the seat itself**, asked of the machine rather than assumed from the tasks that ran
  (`roles/qa_identity/files/qa-identity-proof`, the role's last task): `uid != 0`, no `docker`,
  `root`, `sudo` or `wheel` group, everything `sudo -l -U qa-observer` grants is exactly the one
  wrapper rule, and the account itself cannot read or write `/var/run/docker.sock` (which answers
  group, file mode and ACL in one question). Anything unproved fails the role.

Because both run inside `provision_software.yml`, a failure is an ordinary provisioning failure: the
phase does not complete, `labels.qa_ssh_user` is never written, and the host keeps refusing QA. On
the retrofit path the same failure is recorded as a `provisioning_failed` incident against that
handle with `details.step = qa_identity` and the playbook output that says what was found.

That account cannot become root: its primary group is its own (`qa-observer`, set explicitly, so a
retrofit moves an account somebody created inside `docker` out of it), it is in no secondary group
either, it cannot open the docker socket, and its only sudo rule is
`/usr/local/bin/qa-docker` — a wrapper that refuses every docker sub-command except
`diff, inspect, logs, port, ps, stats, top`. `exec`, `run`, `cp`, `build`, `commit` and the rest are
refused **by the target**, whatever the orchestrator sends. It reads the deployment tree through a
named ACL entry (`u:qa-observer:rx` on `/opt/services`) and can write nothing under it.

**What the run does to the target**: for each run the runtime mints a one-shot ed25519 key and
appends it, with `restrict` and an `expiry-time`, to `qa-observer`'s `authorized_keys` — the file the
provisioning role opened, with a comment line that is never a key. The runtime creates no account and
no file: a target where either is missing refuses the install. The fleet key is used only for that
append and for the removal, which reads the file back to prove the key is gone. The fact that a key
may be installed is written to the QA run's `run_metadata` (`qa_ssh_grant`) *before* the install is
attempted, so an install whose answer is lost still leaves a record; a sweep in `qa-worker`
reconciles every unreleased record every 5 minutes and, after 3 failed attempts, replaces the run's
outcome with a `qa_cleanup_failed` blocker.

**A host with no QA account is refused, visibly.** The run is blocked with `server_unavailable`
(human review; the story is not failed, and health-only criteria still run because they never SSH),
and the reason is written to the provisioning journal as a `provisioning_failed` incident against
that `server_handle` with `details.step = qa_identity`. Both ways of discovering it are journalled
the same way, into the same upserted entry: the label check before anything connects
(`qa_identity_not_provisioned`, `qa_identity_privileged`, `qa_identity_not_attested`) and drift found
on the target afterwards — a row that correctly says `qa-observer` while the account or its
`authorized_keys` has since been deleted (`qa_identity_absent_on_target`). Failures of the QA runtime
itself (no LLM, an agent that dies, an unreachable host) are *not* provisioning facts and stay out of
that journal. That is a normal provisioning incident, so
the host also stops receiving *new* applications until it is repaired — which is the intent: a host
where QA cannot run cannot finish the pipeline. Repair it with the retrofit below; the retrofit
closes the incident.

**What the agent can see** is one capability set, resolved per run from deployment data before any
tool exists (`resolve_capabilities`): the physical root of the deployment directory as the target
resolves it, the containers docker reports for this compose project, the loopback ports allocated to
this application, and the public URL. Every tool in
`services/langgraph/src/agents/qa/tools.py` derives its boundary from that set and from nothing
else — public GET, loopback GET on an allocated port, a file read contained in the physical root,
read-only docker sub-commands against a container of this deployment, container logs/inspect,
Telegram probe. There is no host-wide command (`docker ps`, `df`, `journalctl`): those describe the
machine, which nothing in the set can bound. (`docker ps` is allowed by the target-side wrapper —
resolving the capability set is what needs it — and is exposed as no tool, which is the difference
between the two boundaries: the host limits what may be done, the capability set limits what may be
named.) The agent never holds the run key or the fleet key.

**Already-provisioned servers**: run the retrofit once per host, from the orchestrator:

```bash
docker compose exec infra-service python -m src.provisioner.qa_identity_retrofit vps-267179
```

It runs `playbooks/qa_identity_retrofit.yml`, which creates the same identity from the same
`qa_identity` role a fresh host gets, and removes what the old on-target QA agent left behind in the
administrative account's home.

Cleanup deletes only paths the removed `qa_runner` role itself created, at names nothing else uses:
`~/.local/bin/claude`, `~/.claude/.credentials.json` (the LLM credentials copied onto the target),
`~/.qa-telethon.env`, and `/opt/qa-runner` (venv and the old write-guard script). It touches no
application data and no deployment directory.

Three things are deliberately **left in place**, because that home is also a person's home and they
cannot be told apart from ordinary interactive data:

- `~/.claude` and `~/.local/share/claude` — the Claude Code CLI's own directories, which anybody
  running Claude Code on that host also writes to. Only the credentials file above is certainly the
  platform's.
- `/swapfile` — the old role made 2GB of swap there, but so does every guide an administrator
  follows, and nothing in the file distinguishes them. Taking swap away from a live host running
  user applications is an outage, not cleanup. The swap and its `fstab` entry stay.

Remove those by hand if you know they are the platform's. The playbook's last task prints, per host,
`identity_proof` (what the target said about the account), `removed_paths`, and `left_in_place` —
each surviving path with the reason it survived and the exact command that removes it, e.g.
`swapoff /swapfile && rm -f /swapfile && sed -i '\|^/swapfile|d' /etc/fstab`. A fleet-wide run is
therefore readable per machine, and the decision the playbook refuses to make is handed over with
everything needed to make it.

Only after the playbook succeeds is `labels.qa_ssh_user` written and the host's provisioning-failure
incident resolved — a label written earlier would be a server row telling the QA runtime something
the host cannot back up. Every task is a state, so re-running it changes nothing.

## Deploying

Deploy is triggered manually via GitHub Actions:

1. Go to Actions > "Deploy to Production" > Run workflow
2. The workflow: writes `.env` and secret files, checks out the dispatched revision, pulls and
   verifies the worker base images of that revision, builds service images, starts services, runs
   migrations, verifies health

### Worker base images are a release chain

Every green commit on `main` publishes the whole worker chain to GHCR under that commit's SHA
(`publish-worker-images` in `.github/workflows/ci.yml`, via `infra/scripts/publish-worker-images.sh`).
The tag is the SHA; nothing publishes a mutable `:latest`.

On `main`, "green" includes the required `test-backend-dind-integration` job in the same CI DAG.
`merge-gate` consumes that result before `publish-worker-images` is eligible to run, so a failed,
cancelled, or skipped-on-main Docker-in-Docker worker check cannot write a worker release marker.
The expensive job remains skipped outside `main`; that skip is accepted only there and is never a
release authorization.

**The release is the marker, not the tags.** Four tag pushes cannot be one registry transaction, so
a pushed tag does not mean a commit was released. After all four images resolve, the publish job
writes one more object — `worker-base-release:<sha>`, carrying the digest record of that release
(git SHA, source hash, and every image's `<repository>@sha256:…`). That single write is the
release, and it is the only thing the deploy consults.

| what the registry has for a SHA | what happens |
| --- | --- |
| a marker | released and frozen: re-verify the digests it names, record them, push nothing, exit 0 |
| no marker | not released, whatever image tags exist: build, verify each source hash, push all four, then write the marker |
| a marker naming an image that is gone or built from other sources | refused (exit 7 or 10), never repaired |

The middle row is what a run that failed or was cancelled between two pushes leaves behind. Those
tags are inert residue, not a half-release: nothing will ever deploy them, and **rerunning the
publish job completes that SHA with nobody deleting anything in the registry**. Once the marker
exists the SHA is frozen — rebuilding an already-released commit pushes nothing by design, because
the digests the marker names are what a deploy verifies and replacing them would change what an
already-recorded release means.

The deploy resolves the marker of the revision it is deploying *first*, pulls the digests that
marker names, and checks that every one of them carries `org.codegen.worker_source_hash` equal to
the source hash of that checkout (`infra/scripts/pull-worker-images.sh`). A revision with no marker
(exit 9), a release whose image is gone (exit 3), an image without the label (exit 4) or an image
built from other sources (exit 5) fails the deploy — with the image, the expected hash and the
found hash — before `compose up -d` touches what is running, and before a single local
`worker-base-*:latest` name moves.

Nothing after the marker resolves a tag: the pull, the label check, the local retag and the record
all name the `<repository>@sha256:…` the marker holds. The pull writes its record on the host and
the deploy copies that file back into the run summary and an artifact, so what is reported as
deployed is the release that was verified rather than a second lookup of a mutable tag.

So a commit can only be deployed once its CI publish job has released it. Deploying an unreleased
revision is a refusal, not a fallback to yesterday's workers: that fallback is what put stale
workers onto a green deploy of an exact SHA (GitHub #278).

## First-Time Setup

```bash
# On the prod server as deploy user:

# 1. Clone the repo
sudo mkdir -p /opt/codegen_orchestrator
sudo chown deploy:deploy /opt/codegen_orchestrator
git clone git@github.com:<org>/codegen_orchestrator.git /opt/codegen_orchestrator

# 2. Create directories
sudo mkdir -p /opt/secrets /opt/backups/orchestrator
sudo chown deploy:deploy /opt/secrets /opt/backups/orchestrator

# 3. Install DB backup timer
sudo ln -sf /opt/codegen_orchestrator/infra/systemd/orchestrator-backup.service /etc/systemd/system/
sudo ln -sf /opt/codegen_orchestrator/infra/systemd/orchestrator-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orchestrator-backup.timer

# 4. Verify timer
systemctl list-timers orchestrator-backup

# 5. Check that the deploy user can resolve a tag to a digest. The worker image
#    verification resolves each published tag once and works from that digest.
docker buildx imagetools inspect alpine:3.20 --format '{{.Manifest.Digest}}'

# 6. Run first deploy from GitHub Actions
```

## DB Backup

- Automatic: daily at 03:00 via systemd timer
- Manual: `sudo /opt/codegen_orchestrator/infra/scripts/backup-db.sh`
- Location: `/opt/backups/orchestrator/`
- Retention: last 7 backups
- Restore: `gunzip -c backup.sql.gz | docker compose exec -T db psql -U $POSTGRES_USER $POSTGRES_DB`

## Updating

Standard deploys happen via the GitHub Actions workflow. For manual intervention:

```bash
cd /opt/codegen_orchestrator
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml build
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --remove-orphans
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T api alembic upgrade head
docker image prune -f
```

`worker-manager` and `worker-broker` are one control plane and roll out
together — which the command above does, and the deploy workflow does the same.
Do not restart one alone. They share the worker authorization record: the
manager writes the worker's type when it issues the credential and the broker
authorizes every route from it, so a new broker in front of an old manager
refuses registrations that carry no type, and worker creation fails until the
manager catches up. Worker containers themselves are not Compose services and
deliberately survive the rollout; each service migrates the pre-cutover records
it authorizes on when it starts (`shared/worker_type_cutover.py`).

## Paid-work emergency controls

Use the authenticated admin Settings page for normal operation. It reads and
writes `GET`/`PUT /api/work-admission/controls` as one complete typed state:
emergency stop, maximum concurrent paid runs, and separate engineering and QA
executor overrides. Set an override to `claude` or `codex` only as a
break-glass action for new attempts; reset it to `none` to return to the
project/API legacy policy. Existing queued and running Runs retain their
persisted decision.

For an immediate rollback, set both overrides to `none`, restore the prior
paid-run ceiling, and set `emergency_stop` to `false` only when admissions may
resume. Confirm the committed state by reading the endpoint again. Every changed
field is recorded with its actor, server timestamp, and typed before/after value;
no restart or deploy is needed.

Deploy seeding calls `POST /api/work-admission/controls/initialize` with the
documented defaults. It locks the complete paid-work control set, inserts only
absent rows, and preserves every existing row without an audit fact. A partial
valid state is completed from these defaults; a malformed present value fails
closed and the initialization transaction is rolled back. The deploy seeder
never calls the operator mutation endpoint for these controls.
