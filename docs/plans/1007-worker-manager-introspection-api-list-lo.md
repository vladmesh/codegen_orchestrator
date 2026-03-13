# #1007 Worker-manager introspection API — list, logs, tree, files, prompts, kill

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Admin Panel Phase 2 needs worker introspection. The worker-manager already has a FastAPI app on port 8000 with `/health` and `/api/worker/{id}/infra/compose`. We add a new router with read-only introspection endpoints + kill. Data sources: Redis (`worker:status:*`, `worker:meta:*`, `worker:error:*`, `worker:last_activity:*`), Docker SDK (`list_containers`, `get_container_logs`, `remove_container`), filesystem (`os.walk` on workspace paths).

Key existing code to reuse:
- `WorkerManager.get_worker_status()`, `delete_worker()` — Redis + Docker ops
- `DockerClientWrapper.get_container_logs()`, `list_containers()` — already async-wrapped
- `app.state.redis`, `app.state.docker` — injected in lifespan
- Container naming: `{WORKER_IMAGE_PREFIX}-{worker_id}`, label `com.codegen.worker.id`

Design decisions:
- Add to existing FastAPI app (no separate port) — simpler, reuses lifespan
- New router at `/api/introspect/` to avoid collision with existing `/api/worker/` (which is worker-facing)
- Admin-frontend nginx proxies `/wm-api/` → `worker-manager:8000/api/introspect/`
- Path traversal protection: resolve + check `is_relative_to(workspace_path)`
- No SSE/WebSocket in Phase 2 (logs are GET with `?tail=N`, live streaming deferred to Phase 4)

## Steps

1. [ ] Pydantic response models
   - **Input**: shared/contracts/queues/worker.py (for reference), services/worker-manager/src/
   - **Output**: `services/worker-manager/src/routers/introspect.py` — Pydantic models: `WorkerSummary` (id, status, project_id, workspace_path, dev_network, uptime_seconds, last_activity, error), `WorkerDetail` (extends summary + container_id, image), `WorkerLogsResponse` (worker_id, lines), `FileTreeEntry` (path, is_dir, size), `FileContentResponse` (worker_id, path, content, size), `PromptsResponse` (worker_id, claude_md, task_md)
   - **Test**: unit test that models serialize/deserialize correctly with sample data

2. [ ] GET /workers/ — list active workers
   - **Input**: Redis `worker:status:*` scan, `worker:meta:*` hgetall, `worker:last_activity:*`, `worker:error:*`
   - **Output**: endpoint returns `list[WorkerSummary]` — scans Redis for all `worker:status:*` keys, enriches with metadata
   - **Test**: unit test with fakeredis — seed 3 workers with different statuses, verify list returns all with correct fields

3. [ ] GET /workers/{id} — worker detail
   - **Input**: Redis keys + Docker container inspect (for container_id, image)
   - **Output**: `WorkerDetail` with all fields including container info
   - **Test**: unit test — worker exists → 200 with full detail; worker not found → 404

4. [ ] GET /workers/{id}/logs — container logs
   - **Input**: Docker SDK `get_container_logs(container_name, tail=N)`
   - **Output**: `WorkerLogsResponse` with log lines, query param `?tail=100` (default 100, max 5000)
   - **Test**: unit test with mocked docker — verify tail parameter passed correctly, container not found → 404

5. [ ] GET /workers/{id}/tree — workspace file listing
   - **Input**: `worker:meta:{id}` → workspace_path, `os.walk()` on that path
   - **Output**: `list[FileTreeEntry]` — flat list of files/dirs relative to workspace root
   - **Test**: unit test with tmp_path — create sample file tree, verify output. Test path exists but empty → empty list. Test workspace not found → 404.

6. [ ] GET /workers/{id}/files/{path:path} — file content
   - **Input**: workspace_path + requested path, path traversal check
   - **Output**: `FileContentResponse` with file content (text, max 1MB). Binary files → error.
   - **Test**: unit test with tmp_path — read valid file, attempt `../../etc/passwd` → 403, non-existent file → 404, binary file → 422

7. [ ] GET /workers/{id}/prompts — CLAUDE.md + TASK.md
   - **Input**: workspace_path, read `CLAUDE.md` and `TASK.md` from workspace root
   - **Output**: `PromptsResponse` with content of both (null if missing)
   - **Test**: unit test with tmp_path — both exist, one missing, neither exist

8. [ ] DELETE /workers/{id} — kill worker
   - **Input**: `WorkerManager.delete_worker(worker_id, reason="admin_kill")`
   - **Output**: 204 No Content on success, 404 if not found
   - **Test**: unit test with mocked WorkerManager — verify delete_worker called with correct args

9. [ ] Wire router + app.state.worker_manager
   - **Input**: `services/worker-manager/src/main.py`
   - **Output**: `app.state.worker_manager = worker_manager` added to lifespan, `introspect_router` included in app. All endpoints use `request.app.state.*` for dependencies.
   - **Test**: integration test — start TestClient, hit `/api/introspect/workers/` with fakeredis seeded, verify 200

10. [ ] Nginx proxy for admin-frontend
   - **Input**: `services/admin-frontend/nginx.conf`, `docker-compose.yml`
   - **Output**: Add `location /wm-api/` block proxying to `worker-manager:8000/api/introspect/`. Add worker-manager to admin-frontend's `depends_on`. Verify admin-frontend is on same Docker network as worker-manager.
   - **Test**: manual — `curl http://localhost:3001/wm-api/workers/` through nginx returns JSON

