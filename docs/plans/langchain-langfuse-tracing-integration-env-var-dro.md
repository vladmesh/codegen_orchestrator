# LangChain → Langfuse tracing integration (env-var drop-in)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 3a (task-a51fb1cf) deployed Langfuse v3 infra: langfuse-web, langfuse-worker, ClickHouse, MinIO — all running in docker-compose. Now we wire LangChain/LangGraph agents to send traces there.

**LLM consumers** (4 services, same Docker image):
- `langgraph` (PO consumer) — ChatOpenAI in `agents/po/graph.py` + summarization model
- `architect` — ChatOpenAI in `agents/architect/graph.py`
- `deploy-worker` — LLMFactory in `subgraphs/devops/env_analyzer.py`
- `engineering-worker` — no direct LLM (spawns external coding agents), but traces subgraph execution

**Approach**: Create `CallbackHandler()` from `langfuse` (auto-reads env vars). Pass via `config={"callbacks": [...]}` at `.ainvoke()` sites in each consumer. Agent code stays untouched — only consumer-level wiring changes.

## Steps

1. [ ] Add `langfuse` pip dependency
   - **Input**: `services/langgraph/pyproject.toml`
   - **Output**: `langfuse>=2.0.0` in dependencies list
   - **Test**: `pip install` succeeds, `from langfuse.callback import CallbackHandler` imports cleanly

2. [ ] Create tracing utility module
   - **Input**: env vars `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
   - **Output**: `services/langgraph/src/tracing.py` — exports `get_langfuse_callbacks() -> list[BaseCallbackHandler]`. Returns `[CallbackHandler()]` if env vars present, `[]` otherwise (graceful no-op). Logs whether tracing is enabled at startup.
   - **Test**: unit test — with env vars set → returns handler; without → returns empty list

3. [ ] Wire callbacks into consumers (4 files)
   - **Input**: `consumers/po.py`, `consumers/architect.py`, `consumers/engineering.py`, `consumers/deploy.py`
   - **Output**: Each `.ainvoke()` call passes `config={"callbacks": get_langfuse_callbacks(), ...existing_config}`. For PO consumer (which already has `invoke_config` with `thread_id`), merge callbacks into existing config.
   - **Test**: unit test — mock `get_langfuse_callbacks`, verify callbacks are passed to `ainvoke`

4. [ ] Add Langfuse env vars to docker-compose for all LLM services
   - **Input**: `docker-compose.yml` — services: langgraph, deploy-worker, engineering-worker, architect
   - **Output**: Each service gets: `LANGFUSE_HOST: http://langfuse-web:3000`, `LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY:-}`, `LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY:-}`. Empty defaults = tracing disabled (graceful).
   - **Test**: `docker compose config` shows vars on all 4 services

5. [ ] Update .env.example + enable LANGFUSE_INIT auto-provisioning
   - **Input**: `.env.example`
   - **Output**: Add `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` with clear comments. Uncomment `LANGFUSE_INIT_*` vars with safe defaults so `make up` auto-creates org+project+keys. Update `LANGCHAIN_TRACING_V2` comment to note it's replaced by Langfuse.
   - **Test**: copy .env.example → .env, fill secrets, `make up` starts all services without errors

6. [ ] Lock deps + integration smoke test
   - **Input**: Full stack running (`make up`)
   - **Output**: `make lock-deps` regenerates lockfile. Manual check: trigger a PO message → verify trace appears in Langfuse UI (http://localhost:3002). If LANGFUSE_INIT vars are set, API keys auto-provisioned.
   - **Test**: `make test-unit` passes (no regressions). Langfuse `/api/public/traces` returns at least one trace after PO invocation.

