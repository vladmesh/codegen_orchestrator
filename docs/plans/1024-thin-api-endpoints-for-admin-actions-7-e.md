# #1024 Thin API endpoints for admin actions (7 endpoints)

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Phase 3 of the Admin Panel v2 brainstorm (bs-d124d343). Prerequisites are done: #1023 (queue contracts: optional story_id + action field) and #1006 (decouple deploy worker from story lifecycle). The API is currently a pure CRUD layer with no Redis publishing — all queue publishing happens in the scheduler service. This task adds 7 thin action endpoints + 1 delete endpoint that follow the pattern: validate → DB write → Redis publish.

Key current state:
- `DeployMessage` has `action: DeployAction` (CREATE/FEATURE/FIX/STOP/UNDEPLOY), `story_id` optional
- `QAMessage` has `story_id` optional, `run_id` optional
- `EngineeringMessage` has `story_id` optional, `planning_task_id` for status updates
- `ArchitectMessage` has `story_id`, `project_id`, `user_id`, `is_reopen`
- `ApplicationStatus` enum: NOT_DEPLOYED, RUNNING, STOPPED, DOWN, DEGRADED — missing transitional states (DEPLOYING, STOPPING, UNDEPLOYING)
- `DeployTrigger` enum: ENGINEERING, WEBHOOK, PO — missing ADMIN
- API has no `RedisStreamClient` — debug router uses raw `aioredis` connections
- Task/Story action endpoints use `TaskTransition`/`StoryTransition` body DTOs with actor/reason/details
- Sibling tasks: #1026 (Admin UI action buttons) depends on these endpoints

## Steps

1. [ ] Add Redis publishing capability to API service
   - **Input**: `services/api/src/dependencies.py` (new), `services/api/src/config.py`
   - **Output**: FastAPI dependency `get_redis_client()` that returns a `RedisStreamClient` singleton, initialized on app startup and closed on shutdown. Use lifespan or app events.
   - **Test**: Unit test — dependency returns connected client; service test — publish a message and verify it lands in the stream.

2. [ ] Extend shared contracts for admin actions ⚠️ needs-approval
   - **Input**: `shared/contracts/dto/application.py`, `shared/contracts/queues/deploy.py`
   - **Output**: (a) Add `DEPLOYING`, `STOPPING`, `UNDEPLOYING` to `ApplicationStatus` enum. (b) Add `ADMIN` to `DeployTrigger` enum.
   - **Test**: Unit test — new enum values serialize correctly, DeployMessage with trigger=ADMIN validates.

3. [ ] Create request schemas for action endpoints
   - **Input**: `services/api/src/schemas/` (new file `actions.py` or extend existing)
   - **Output**: Pydantic models for request bodies: `SendToArchitectRequest(actor: str)`, `SpawnWorkerRequest(actor: str, description: str | None)`, `FromRepoRequest(repo_url: str, project_id: UUID, server_handle: str, service_name: str)`, `RunE2ERequest(actor: str)`. Simple actions (stop, undeploy, redeploy) use no body or a minimal `AdminAction(actor: str)`.
   - **Test**: Unit test — schema validation (required fields, types).

4. [ ] POST /stories/{id}/send-to-architect endpoint
   - **Input**: `services/api/src/routers/stories.py`, story VALID_TRANSITIONS
   - **Output**: Validates story exists + status in (CREATED, REOPENED). Transitions story → IN_PROGRESS. Publishes `ArchitectMessage` to `architect:queue`. Returns updated story.
   - **Test**: Service test — create story (CREATED), call endpoint, verify status=IN_PROGRESS + message in architect:queue.

5. [ ] POST /tasks/{id}/spawn-worker endpoint
   - **Input**: `services/api/src/routers/_task_actions.py` or new file
   - **Output**: Validates task exists + status suitable (BACKLOG/TODO/IN_DEV). Transitions → IN_DEV if needed. Creates Run (type=engineering, status=queued). Publishes `EngineeringMessage` to `engineering:queue` with `planning_task_id=task.id`. Returns `{task: TaskRead, run: RunRead}`.
   - **Test**: Service test — create task (BACKLOG), call endpoint, verify task status=IN_DEV + Run created + message in engineering:queue.

6. [ ] Application action endpoints (stop, undeploy, redeploy)
   - **Input**: `services/api/src/routers/applications.py`
   - **Output**: Three POST endpoints on existing applications router:
     - `/applications/{id}/stop` — set status→STOPPING, publish DeployMessage(action=STOP)
     - `/applications/{id}/undeploy` — set status→UNDEPLOYING, publish DeployMessage(action=UNDEPLOY)
     - `/applications/{id}/redeploy` — create Deployment record, publish DeployMessage(action=CREATE)
     All three need application.repo → repository → project chain to populate DeployMessage fields.
   - **Test**: Service test per action — create app, call endpoint, verify status change + message in deploy:queue with correct action.

7. [ ] POST /applications/{id}/run-e2e endpoint
   - **Input**: `services/api/src/routers/applications.py`
   - **Output**: Validates application exists + status=RUNNING. Creates Run (type=qa, status=queued). Needs to resolve `deployed_url` from application (server IP + port). Publishes `QAMessage` to `qa:queue`. Returns `{application: ApplicationRead, run: RunRead}`.
   - **Test**: Service test — create running app with port, call endpoint, verify Run created + message in qa:queue.

8. [ ] POST /applications/from-repo endpoint
   - **Input**: `services/api/src/routers/applications.py`
   - **Output**: Creates Repository (if not exists by git_url), creates Application, allocates port on server, publishes DeployMessage(action=CREATE) to deploy:queue. Atomically in one transaction. Returns `{application: ApplicationRead, repository: RepositoryRead}`.
   - **Test**: Service test — call with repo_url + project_id + server_handle, verify Repository + Application + PortAllocation created + message in deploy:queue.

9. [ ] DELETE /projects/{id}/config/secrets/{key} endpoint
   - **Input**: `services/api/src/routers/projects.py`
   - **Output**: Removes a single key from project config secrets. Uses SELECT FOR UPDATE (same pattern as merge_secrets). Returns remaining keys list.
   - **Test**: Service test — merge 2 secrets, delete 1, verify only 1 remains.

10. [ ] Integration tests for all action endpoints
    - **Input**: All new endpoints
    - **Output**: Full flow tests: create prerequisite entities → call action endpoint → verify DB state + Redis message content. Cover error cases: missing entity (404), invalid status transition (422), missing required fields (422).
    - **Test**: ~15-20 test cases covering happy paths and error paths for all 8 endpoints.

