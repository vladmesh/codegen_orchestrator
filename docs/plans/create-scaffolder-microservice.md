# Create scaffolder microservice

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

The current pipeline runs scaffold INSIDE the worker container (worker-manager._run_scaffold_phase).
Per PIPELINE_V2, scaffolding should happen BEFORE the architect runs, so the architect can see the
project tree and create tasks for the diff (not from scratch). This task creates a standalone
scaffolder service that consumes from scaffold:queue and runs copier + make setup + git push
directly on the host filesystem.

**Brainstorm**: bs-d302b6a1 (Architect Context & Worker Knowledge)
**Pipeline spec**: docs/PIPELINE_V2.md — Phase 2

**Current state**: No scaffolder service exists. Scaffold logic is in worker-manager (lines 585-862
of manager.py). No scaffold:queue defined in shared/queues.py. ProjectStatus already has
SCAFFOLDING/SCAFFOLDED/SCAFFOLD_FAILED states.

## Steps

1. [ ] Add scaffold queue constants and message contract
   - **Input**: shared/queues.py, shared/contracts/queues/
   - **Output**: SCAFFOLD_QUEUE="scaffold:queue", SCAFFOLD_GROUP="scaffold-consumers" in shared/queues.py + QueueBinding. New ScaffoldMessage in shared/contracts/queues/scaffold.py (project_id, repository_id, user_id, template_repo, project_name, modules, task_description)
   - **Test**: Unit test validates ScaffoldMessage serialization/deserialization, queue constants exist

2. [ ] Create scaffolder service skeleton (config, Dockerfile, pyproject.toml, docker-compose entry)
   - **Input**: services/scheduler/ as reference pattern, docker-compose.yml
   - **Output**: services/scaffolder/ with Dockerfile (multi-stage, python:3.12-slim + uv + git + make), pyproject.toml (httpx, redis, structlog, pydantic), src/__init__.py, src/config.py (Settings with redis_url, api_base_url, workspace_base_path, service_template_path), src/main.py (entry point). Docker-compose entry with volume mount for /data/workspaces and service-template.
   - **Test**: Unit test for config validation (missing env vars raise RuntimeError)

3. [ ] Implement scaffold logic (core business logic, no queue/IO)
   - **Input**: worker-manager _run_scaffold_phase as reference, services/scaffolder/src/
   - **Output**: src/scaffold.py with async function run_scaffold(config: ScaffoldMessage, workspace_path: Path, settings: Settings) -> ScaffoldResult. Steps: create GitHub repo if needed (httpx), clone into workspace, copier copy, make setup, git push, capture tree output, return tree string. Pure logic, no Redis/API updates.
   - **Test**: Unit tests with mocked subprocess calls — verify copier command args, make setup called, git push called, tree captured. Test failure paths (copier fails, make setup fails, git push fails).

4. [ ] Implement consumer (process_scaffold_job) + API client calls
   - **Input**: services/langgraph/src/consumers/_base.py as pattern, src/scaffold.py
   - **Output**: src/consumer.py with process_scaffold_job(job_data, redis) -> dict. Validates ScaffoldMessage, sets project.status=scaffolding via API, calls run_scaffold(), on success: PATCH project status=scaffolded + save tree to repository config via API. On failure: PATCH project status=scaffold_failed. src/main.py wires start_worker(). src/clients/api.py — thin httpx client for project/repo PATCH.
   - **Test**: Unit tests with mocked scaffold.run_scaffold and mocked API client — verify status transitions (scaffolding->scaffolded, scaffolding->scaffold_failed), tree saved to API.

5. [ ] Wire scheduler to publish to scaffold:queue
   - **Input**: services/scheduler/src/tasks/task_dispatcher.py, API project/story endpoints
   - **Output**: New function in scheduler that detects unscaffolded projects with stories ready (project.status == draft + has stories) and publishes ScaffoldMessage to scaffold:queue. Add to dispatcher loop or as separate periodic task. Guard: do not re-publish if status is already scaffolding.
   - **Test**: Unit test — mock API returns project with status=draft + story -> ScaffoldMessage published. Project already scaffolding -> skip.

6. [ ] Integration test: scaffold queue -> consumer -> API updates
   - **Input**: All previous steps
   - **Output**: tests/integration/test_scaffold_flow.py — end-to-end test with real Redis, mocked subprocess (copier/make/git), mocked API. Publish ScaffoldMessage -> consumer processes -> verify API PATCH calls for status transitions and tree save.
   - **Test**: Integration test itself (requires Redis container)

