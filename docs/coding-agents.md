# External Coding Agents

For development tasks we use production-ready tools instead of writing our own agents.

Three are implemented and interchangeable: Claude Code, Factory.ai Droid and OpenAI Codex.
A project picks one at creation time; when it does not, `DEFAULT_AGENT_TYPE` decides, and that
default is `claude`.

## Claude Code

The default. A CLI tool from Anthropic for agentic coding.

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

Codex is available only for developer workers. The image pins Codex CLI
`0.144.6`; the wrapper runs it non-interactively:

```bash
codex exec --sandbox workspace-write \
  --config sandbox_workspace_write.network_access=true \
  "Read TASK.md and AGENTS.md, then complete the task described in TASK.md."
```

The task is in `/workspace/TASK.md`, and the shared developer instructions are
in `/workspace/AGENTS.md`. The agent must report success or failure through
`POST http://localhost:9090/result`. CLI stdout and stderr are diagnostics and
are neither accepted as the business result nor persisted for Codex workers.
The per-run network override is required because `workspace-write` otherwise
blocks the agent's localhost result call, dependency access, and Git push. The
Docker worker network remains the outer isolation boundary.

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
tokens persist. Claude, Factory, and noop workers do not receive this mount.

See the official [authentication](https://learn.chatgpt.com/docs/auth) and
[non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
documentation for the upstream behavior.

---

## Integration into the project

The Developer node in the Engineering Subgraph uses coding agents through the `worker-manager` service (the PO does not use containers, it is a LangGraph ReactAgent):

When creating a project the PO passes the chosen developer worker in
`create_project(agent_type="claude" | "factory" | "codex")`. The value is stored
in `project.config.agent_type` and applies to the engineering tasks of that project.
If no choice is given, `claude` is used. An unknown value is rejected before
the project is created.

1. Worker-manager creates a container from a worker-base image
2. Mounts the pre-scaffolded workspace (`/data/workspaces/{repo_id}/`) — the code is already in place
3. Worker-manager creates/checks out story feature branch (`story/{story_id}`)
4. Injects the static instructions from `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` → an agent-specific file (`CLAUDE.md` / `AGENTS.md`)
5. Injects a dynamic `TASK.md` into `/workspace/TASK.md` with the project-specific task. Previous tasks archived in `.story/old_tasks/`
6. Starts the coding agent (Claude Code, Droid or Codex) in non-interactive mode
7. The agent commits and pushes to the feature branch. Worker-wrapper pulls from the current branch (not a hardcoded `main`)
8. The agent reports the result over HTTP: `curl -X POST localhost:9090/result -d '{"success":true,"commit":"<sha>","summary":"..."}'`
9. If the task cannot be completed: `curl -X POST localhost:9090/result -d '{"success":false,"reason":"..."}'`

**The worker-wrapper HTTP server** (`localhost:9090`):
- `POST /result` — a single endpoint for results (success/failure). Auto-resume: if the agent exits without calling `/result`, the wrapper restarts it once automatically.
- `POST /infra/compose` — a compose proxy for managing the sidecar infrastructure (db, redis). Proxied to worker-manager.
- The Makefile override targets (`make migrate`, `make dev-start`) inside the worker use `curl localhost:9090/infra/compose`.

---

## Mapping onto the graph nodes

| Node | Tool | Status |
|------|------------|--------|
| **Scaffolder** | Copier template | ✅ Implemented |
| **Developer** | Claude Code / Factory.ai Droid / OpenAI Codex | ✅ Implemented (Native execution, Flat Dev Environment) |
| **Tester** | — | ❌ Removed. The Developer runs the tests through `make`; CI checks run after the subgraph |
| **DevOps** | GitHub Actions (deploy.yml) | ✅ Implemented |
