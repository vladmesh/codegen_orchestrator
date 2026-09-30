# External Coding Agents

For development tasks we use production-ready tools instead of writing our own agents.

Three are implemented and interchangeable: Claude Code, Factory.ai Droid and OpenAI Codex.
A project picks one at creation time; when it does not, the required `DEFAULT_AGENT_TYPE`
setting decides. It has no fallback; deployments set it explicitly (production uses `claude`).

## Claude Code

The production choice. A CLI tool from Anthropic for agentic coding.

```bash
# Installation (native installer)
curl -fsSL https://claude.ai/install.sh | sh

# Usage
claude -p "Implement user registration endpoint"

# Pipe
cat error.log | claude -p "Fix this error"
```

**Context:** natively uses `CLAUDE.md` files. Worker-manager automatically maps `INSTRUCTIONS.md` → `CLAUDE.md`.

**Price:** a Pro/Max subscription, cheaper than the API. Workers authenticate through their own
session, separate from the operator's.

## Factory.ai Droid

An autonomous coding agent with autonomy levels: low (many confirmations), medium, high (full
autonomy). The worker runs it non-interactively:

```bash
droid exec --prompt-file TASK.md --skip-permissions-unsafe
```

## OpenAI Codex CLI

Codex is available for developer workers and is the default central exploratory-QA executor. The image pins Codex CLI
`0.144.6`; the wrapper runs it non-interactively:

```bash
codex exec --sandbox danger-full-access \
  "Read TASK.md and WORKER_INSTRUCTIONS.md, then complete the task described in TASK.md."
```

The task is in `/workspace/TASK.md`, and the shared developer instructions are
in `/workspace/WORKER_INSTRUCTIONS.md` — not in the product's own `AGENTS.md`,
which is a tracked file of the Copier kit and belongs to the product. The agent must report success or failure through
`POST http://localhost:9090/result`. CLI stdout and stderr are diagnostics and
are neither accepted as the business result nor persisted for Codex workers.
Codex's own sandbox is off because the container already is one, and the two
cannot nest: `workspace-write` puts every file operation through Codex's bwrap
helper, which needs a user namespace the worker container does not have. Codex
then fails to read `TASK.md`, reports itself blocked and exits without a result.
`danger-full-access` also removes the need for the per-run network override that
`workspace-write` required for the localhost result call, dependency access and
Git push. The Docker worker network and the container's own `cap_drop: ALL`,
`no-new-privileges` and resource limits remain the isolation boundary.

A central QA worker is intentionally different: it receives an empty ephemeral
non-Git workspace, injected `WORKER_INSTRUCTIONS.md` and `TASK.md`, and invokes Codex with
`--skip-git-repo-check`. Its deployment access is the QA capability endpoint
only; the target never receives the mounted Codex profile or an API key.

### Dedicated ChatGPT session profile

Do not mount the operator's live `~/.codex`. Create a separate profile on the
Docker host and log in once with device authentication:

```bash
install -d -m 0700 "$HOME/.codex-worker"
printf 'cli_auth_credentials_store = "file"\n' > "$HOME/.codex-worker/config.toml"
chmod 0600 "$HOME/.codex-worker/config.toml"
CODEX_HOME="$HOME/.codex-worker" codex login --device-auth
chmod 0600 "$HOME/.codex-worker/auth.json"
```

Set `HOST_CODEX_HOME=/home/youruser/.codex-worker` in `.env`, then rebuild the
worker images. Worker-manager requires directory mode `0700`, file modes
`0600`, access and refresh tokens in a valid `auth.json`, and
`cli_auth_credentials_store = "file"`. A missing or unsuitable profile stops
Codex worker creation before image resolution. The profile is mounted
read-write only into Codex containers at `/home/worker/.codex` so refreshed
tokens persist. A profile-local advisory lock covers each complete Codex
host-session command, intentionally serializing workers that share one profile
so simultaneous refreshes cannot corrupt `auth.json`. Claude, Factory, and
noop workers do not receive this mount.

The profile has three consumers under that one lock: the Codex workers, and the
`codex` LLM channel of the `langgraph` (PO, PO summarizer) and `architect`
containers ([NODES.md](NODES.md#-llm-channel-chain-architect-po-po-summarizer)).
Compose mounts the same `HOST_CODEX_HOME` read-write into both at
`/llm-codex-home`. Their `codex exec` holds `.codegen-codex.lock` exclusively
for the whole call, exactly as the wrapper does, and runs as the profile
directory's owner rather than as the container's root, so a refresh leaves an
`auth.json` the workers can still read; a lock these containers create is
handed to that owner before it appears. They hold the profile to the rules
above and refuse it (`missing_credential`, with the reason) when the directory
is not `0700`, is owned by root, lacks a `0600` `auth.json` owned by the same
user, or lacks a `0600` `config.toml` with `cli_auth_credentials_store =
"file"`. They never copy it and run the same pinned CLI version as the workers,
because one profile written by two versions could end in an `auth.json` one of
them cannot load.

Worker-manager also reads this profile passively for executor diagnostics: token
presence, the access token's `exp` claim, a refresh-token `exp` only when that
token is a JWT, and `last_refresh`. It never runs Codex against the profile or a
copy of it; a copied profile that refreshes rotates the refresh token and breaks
the real one. Login state, expiry and the administrator alert are described in
[live-deploy-operations.md](live-deploy-operations.md#log-in-the-production-subscription-executor-profiles).

See the official [authentication](https://learn.chatgpt.com/docs/auth) and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
documentation for the upstream behavior.

---

## Integration into the project

The Developer node in the Engineering Subgraph uses coding agents through the `worker-manager` service (the PO does not use containers, it is a LangGraph ReactAgent):

When creating a project the PO passes the chosen developer worker in
`create_project(agent_type="claude" | "factory" | "codex")`. The value is stored
in `project.config.agent_type` and applies to the engineering tasks of that project.
If no choice is given, the API resolves the current runtime
`DEFAULT_AGENT_TYPE` when the project is created. An unknown value is rejected
before the project is created.

1. Worker-manager creates a container from a worker-base image
2. Mounts the pre-scaffolded workspace (`/data/workspaces/{repo_id}/`) — the code is already in place
3. Worker-manager creates/checks out story feature branch (`story/{story_id}`)
4. Injects the static instructions from `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` → an agent-specific file (`CLAUDE.md` for Claude, `WORKER_INSTRUCTIONS.md` for Codex and Droid). Both names, and every other path a turn writes into the checkout, are defined once in `shared/constants.py::WorkerWorkspace` and kept out of the product's commits by `packages/worker-wrapper/src/worker_wrapper/injected_paths.py`: the wrapper writes them to the workspace-local `.git/info/exclude`, and its publish guard refuses a commit that carries one anyway. Nothing the product tracks — its `Makefile`, `AGENTS.md` or `.gitignore` — is written by a turn; worker-mode compose reaches the proxy through `DOCKER_COMPOSE` instead (`worker_wrapper/compose_proxy.py`).
5. Injects a dynamic `TASK.md` into `/workspace/TASK.md` with the project-specific task
6. Starts the coding agent (Claude Code, Droid or Codex) in non-interactive mode
7. The agent commits and pushes to the feature branch. Worker-wrapper pulls from the current branch (not a hardcoded `main`)
8. The agent reports the result over HTTP: `curl -X POST localhost:9090/result -d '{"success":true,"commit":"<sha>","summary":"..."}'`
9. If the task cannot be completed: `curl -X POST localhost:9090/result -d '{"success":false,"reason":"..."}'`

**The worker-wrapper HTTP server** (`localhost:9090`):
- `POST /result` — a single endpoint for results (success/failure). Auto-resume: if the agent exits without calling `/result`, the wrapper restarts it once automatically.
- `POST /infra/compose` — a compose proxy for managing the sidecar infrastructure (db, redis). Proxied to worker-manager.
- The Makefile override targets (`make migrate`, `make dev-start`) inside the worker use `curl localhost:9090/infra/compose`.

### Agent subprocess environment

The wrapper does not pass its complete container environment to Claude, Codex,
Factory or noop subprocesses. Both the normal launch and Claude auto-resume use
the same allowlist: process basics (`HOME`, `PATH`, locale, terminal, temporary
directory, timezone and `PYTHONPATH` without `/app`); `PYTHONNOUSERSITE`, which
keeps agent Python commands from loading user-site packages that can shadow the
wrapper's `shared` package; Claude authentication and session settings
(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`CLAUDE_CONFIG_DIR`) plus the container runtime settings
`DISABLE_AUTOUPDATER` and `DISABLE_TELEMETRY`; Codex authentication and session
settings (`CODEX_API_KEY`, `CODEX_HOME`); `FACTORY_API_KEY`; and the
repository-scoped `GITHUB_TOKEN` and `GH_TOKEN` credentials. The cosmetic
interpreter settings `PYTHONUNBUFFERED` and `PYTHONDONTWRITEBYTECODE` remain
wrapper-only. The agent uses `localhost:9090` for result reporting and Compose
operations, so the child `env=` mapping does not pass wrapper
Redis/API/manager URLs, encryption keys, Docker/Compose sockets, host paths or
arbitrary task command environment variables. Saved transcripts still redact
secrets from the full wrapper environment rather than the reduced child
environment.

---

## Mapping onto the graph nodes

| Node | Tool | Status |
|------|------------|--------|
| **Scaffolder** | Copier template | ✅ Implemented |
| **Developer** | Claude Code / Factory.ai Droid / OpenAI Codex | ✅ Implemented (Native execution, Flat Dev Environment) |
| **DevOps** | GitHub Actions (deploy.yml) | ✅ Implemented |
