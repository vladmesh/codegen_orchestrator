# Refactor large files (>400 LOC) — extract helpers

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Audit found 10 files exceeding 400 LOC limit. Combined they total ~7,000 LOC with avg 687 LOC/file.
Goal: extract helper functions/classes into focused modules (100-250 LOC each), reducing complexity scores.
Approach: incremental per-file, largest first, grouped by service. Each step is one file extraction.

No shared/contracts or DB schema changes needed — pure refactoring.

## Steps

1. [ ] Extract helpers from `services/worker-manager/src/manager.py` (920 LOC)
   - **Input**: `services/worker-manager/src/manager.py`
   - **Output**: New modules: `src/garbage_collector.py` (GC methods), `src/image_manager.py` (image cache/build), `src/git_setup.py` (clone/token/branch), `src/scaffold_phase.py` (copier+make). Manager imports and delegates.
   - **Test**: `make test-worker-manager-unit` — existing tests pass, add unit tests for each extracted class

2. [ ] Extract helpers from `services/langgraph/src/consumers/engineering.py` (881 LOC)
   - **Input**: `services/langgraph/src/consumers/engineering.py`
   - **Output**: New modules: `consumers/story_context.py` (StoryContextBuilder — story.md + context assembly), `consumers/engineering_result_handler.py` (success/blocked/reject routing). Consumer becomes thin dispatcher.
   - **Test**: `make test-langgraph-unit` — existing tests pass, add unit tests for extracted modules

3. [ ] Extract helpers from `services/langgraph/src/consumers/deploy.py` (866 LOC)
   - **Input**: `services/langgraph/src/consumers/deploy.py`
   - **Output**: New modules: `consumers/deploy_failure_handler.py` (classification + CODE_FIX/RETRY/GIVE_UP routing), `consumers/deploy_precheck.py` (SSH validation). Consumer becomes thin dispatcher.
   - **Test**: `make test-langgraph-unit` — existing tests pass, add unit tests for extracted modules

4. [ ] Extract helpers from `services/scheduler/src/tasks/task_dispatcher.py` (738 LOC)
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: New modules: `tasks/story_completer.py` (story completion + PR creation), `tasks/pipeline_supervisor.py` (stuck story/task/failure supervision), `tasks/pr_poller.py` (merged PR polling + deploy trigger). Main loop orchestrates the four concerns.
   - **Test**: `make test-scheduler-unit` — existing tests pass, add unit tests for each extracted module

5. [ ] Extract helpers from `services/api/src/routers/rag.py` (689 LOC)
   - **Input**: `services/api/src/routers/rag.py`
   - **Output**: New modules: `routers/rag_ingestion.py` (chunking + embedding + upsert), `routers/rag_query.py` (vector search + token budget). Router keeps thin endpoint definitions.
   - **Test**: `make test-api-unit` — existing tests pass, add unit tests for extracted modules

6. [ ] Extract helpers from `services/infra-service/src/provisioner/node.py` (642 LOC)
   - **Input**: `services/infra-service/src/provisioner/node.py`
   - **Output**: New modules: `provisioner/password_manager.py` (Time4VPS password reset), `provisioner/reinstall_flow.py` (OS reinstall + Ansible). ProvisionerNode delegates to extracted classes.
   - **Test**: Unit tests for each extracted class

7. [ ] Extract helpers from `services/langgraph/src/subgraphs/devops/nodes.py` (639 LOC)
   - **Input**: `services/langgraph/src/subgraphs/devops/nodes.py`
   - **Output**: New modules: `devops/secret_resolver.py` (four-phase secret resolution), `devops/deployment_recorder.py` (app creation + deployment records), `devops/github_secrets_writer.py` (GH Actions secret injection). Nodes file keeps thin node functions.
   - **Test**: `make test-langgraph-unit` — existing tests pass, add unit tests for extracted modules

8. [ ] Extract helpers from `services/api/src/routers/tasks.py` (625 LOC)
   - **Input**: `services/api/src/routers/tasks.py`
   - **Output**: New modules: `routers/task_transitions.py` (state machine validation + event creation), `routers/task_queries.py` (filter parsing + query construction). Router keeps endpoint definitions.
   - **Test**: `make test-api-unit` — existing tests pass, add unit tests for extracted modules

9. [ ] Extract helpers from `services/langgraph/src/agents/po/tools.py` (605 LOC)
   - **Input**: `services/langgraph/src/agents/po/tools.py`
   - **Output**: New modules: `po/project_tools.py` (project CRUD), `po/story_tools.py` (story CRUD + reopen), `po/secret_tools.py` (secret management). Main tools.py re-exports for backward compat.
   - **Test**: `make test-langgraph-unit` — existing tests pass

10. [ ] Extract helpers from `services/langgraph/src/nodes/developer.py` (513 LOC)
    - **Input**: `services/langgraph/src/nodes/developer.py`
    - **Output**: New module: `nodes/task_message_builder.py` (task message assembly — largest single function). DeveloperNode stays lean.
    - **Test**: `make test-langgraph-unit` — existing tests pass, add unit test for TaskMessageBuilder

11. [ ] Integration verification — full test suite
    - **Input**: All modified services
    - **Output**: `make test-unit` passes with 0 failures
    - **Test**: `make test-unit` (all services), verify no import errors via `make lint`

