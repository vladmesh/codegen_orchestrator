# Внешние Coding Agents

Для задач разработки используем production-ready инструменты вместо написания своих агентов.

## Текущая реализация: Factory.ai Droid

Автономный coding agent с уровнями автономности.

```bash
# Интерактивный режим
droid

# Single-shot (для автоматизации)
droid exec "Implement feature X" --autonomy high

# Из файла (используется в coding-worker)
droid exec --prompt-file TASK.md --skip-permissions-unsafe
```

**Autonomy levels:** low (много подтверждений), medium, high (полная автономия).

## Claude Code

Claude Code — CLI-инструмент от Anthropic для agentic coding. Реализован наравне с Droid.

```bash
# Установка (native installer)
curl -fsSL https://claude.ai/install.sh | sh

# Использование
claude -p "Implement user registration endpoint"

# Pipe
cat error.log | claude -p "Fix this error"
```

**Контекст:** Нативно использует `CLAUDE.md` файлы. Worker-manager автоматически маппит `INSTRUCTIONS.md` → `CLAUDE.md`.

**Цена:** Pro/Max подписка (~$20-100/мес), дешевле чем API.

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

## Интеграция в проект

Developer node в Engineering Subgraph использует coding agents через `worker-manager` сервис (PO не использует контейнеры — это LangGraph ReactAgent):

1. Worker-manager создаёт контейнер из worker-base образа
2. Монтирует pre-scaffolded workspace (`/data/workspaces/{repo_id}/`) — код уже на месте
3. Worker-manager creates/checks out story feature branch (`story/{story_id}`)
4. Инжектит статические инструкции из `services/langgraph/src/prompts/developer_worker/INSTRUCTIONS.md` → agent-specific file (`CLAUDE.md` / `AGENTS.md`)
5. Инжектит динамический `TASK.md` в `/workspace/TASK.md` с project-specific задачей. Previous tasks archived in `.story/old_tasks/`
6. Запускает coding agent (Droid, Claude Code или Codex) в non-interactive режиме
7. Агент коммитит и пушит на feature branch. Worker-wrapper pulls from current branch (not hardcoded `main`)
8. Агент сообщает результат через HTTP: `curl -X POST localhost:9090/result -d '{"success":true,"commit":"<sha>","summary":"..."}'`
9. При невозможности выполнения: `curl -X POST localhost:9090/result -d '{"success":false,"reason":"..."}'`

**Worker-wrapper HTTP сервер** (`localhost:9090`):
- `POST /result` — единый endpoint для результатов (success/failure). Auto-resume: если агент завершается без вызова `/result`, wrapper автоматически перезапускает его один раз.
- `POST /infra/compose` — compose proxy для управления sidecar-инфраструктурой (db, redis). Проксируется в worker-manager.
- Makefile override targets (`make migrate`, `make dev-start`) внутри воркера используют `curl localhost:9090/infra/compose`.

---

## Маппинг на узлы графа

| Узел | Инструмент | Статус |
|------|------------|--------|
| **Scaffolder** | Copier template | ✅ Реализовано |
| **Developer** | Factory.ai Droid / Claude Code / OpenAI Codex | ✅ Реализовано (Native execution, Flat Dev Environment) |
| **Tester** | Функциональный узел (запуск тестов) | ⚠️ Заглушка (временно Developer сам запускает тесты через `make`) |
| **DevOps** | GitHub Actions (deploy.yml) | ✅ Реализовано |
