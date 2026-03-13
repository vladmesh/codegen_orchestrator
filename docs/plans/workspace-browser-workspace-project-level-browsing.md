# Workspace browser — workspace как первичная сущность с project-level browsing

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Source brainstorm: bs-6ad7e94f (workspace-browser.md)

Currently workspace file browsing is tied to worker_id via introspect API endpoints in worker-manager. But workspaces belong to projects (reused across workers, survive worker death for up to 35h). Goal: make workspace a first-class entity keyed by project_id, with worker delegating to it.

Key files:
- `services/worker-manager/src/routers/introspect.py` — current worker-bound tree/files/prompts endpoints
- `services/worker-manager/src/workspace.py` — path resolution helpers
- `services/worker-manager/src/config.py` — WORKSPACE_BASE_PATH, SCAFFOLDED_WORKSPACE_PATH settings
- `services/worker-manager/src/main.py` — router registration
- `services/worker-manager/tests/unit/test_introspect_api.py` — existing tests (396 LOC)
- `services/admin-frontend/src/pages/WorkerDetailPage.tsx` — FilesTab with TreeNodeView, buildTree, formatSize
- `services/admin-frontend/src/pages/ProjectDetailPage.tsx` — needs Workspace tab
- `services/admin-frontend/src/types/api.ts` — FileTreeEntry, FileContentResponse types
- `services/admin-frontend/nginx.conf` — `/wm-api/` proxy already covers new endpoints

## Steps

1. [ ] Backend: workspace introspection router
   - **Input**: `services/worker-manager/src/routers/introspect.py` (reuse `_safe_resolve`, `FileTreeEntry`, `MAX_FILE_SIZE`), `services/worker-manager/src/config.py`
   - **Output**: New file `services/worker-manager/src/routers/workspaces.py` with:
     - `GET /api/introspect/workspaces/{project_id}/tree` — resolve workspace path, `os.walk`, return `list[FileTreeEntry]`
     - `GET /api/introspect/workspaces/{project_id}/files/{file_path:path}` — read file with path traversal protection
     - Helper `_resolve_workspace_path(project_id)`: check `WORKSPACE_BASE_PATH/{project_id}/workspace/` first, then fallback to `SCAFFOLDED_WORKSPACE_PATH/{repo_id}/` (where repo_id = project_id for now), 404 if neither exists
     - New response model `WorkspaceFileContentResponse` (like `FileContentResponse` but with `project_id` instead of `worker_id`)
   - Extract shared helpers (`_safe_resolve`, `FileTreeEntry`, `MAX_FILE_SIZE`) into `services/worker-manager/src/routers/_shared.py` to avoid circular imports
   - Register router in `services/worker-manager/src/main.py`
   - **Test**: `services/worker-manager/tests/unit/test_workspaces_api.py` — tree returns entries, file read works, path traversal blocked, workspace not found → 404

2. [ ] Backend: worker tree/files delegate to workspace
   - **Input**: `services/worker-manager/src/routers/introspect.py`
   - **Output**: Refactor `get_worker_tree` and `get_worker_file` to:
     - Look up `project_id` from `worker:meta:{worker_id}`
     - If project_id exists → resolve workspace via same logic as workspace router (`_resolve_workspace_path`)
     - If no project_id → fall back to current behavior (workspace_path from Redis meta)
     - This keeps backward compatibility for ephemeral workers without project_id
   - `/prompts` stays per-worker (unchanged)
   - **Test**: Update `test_introspect_api.py` — worker with project_id resolves workspace via project path, worker without project_id still works via meta workspace_path

3. [ ] Frontend: extract shared workspace components
   - **Input**: `services/admin-frontend/src/pages/WorkerDetailPage.tsx` (TreeNode, TreeNodeView, buildTree, formatSize, FilesTab)
   - **Output**: 
     - `services/admin-frontend/src/components/workspace/FileTree.tsx` — TreeNode type, buildTree fn, TreeNodeView component
     - `services/admin-frontend/src/components/workspace/FileViewer.tsx` — file content panel with header (path, size) and pre block
     - `services/admin-frontend/src/components/workspace/WorkspaceBrowser.tsx` — composite component that takes `treeApiUrl` and `fileApiUrlPrefix` props, manages selectedFile state, renders FileTree + FileViewer side by side
     - `services/admin-frontend/src/components/workspace/index.ts` — barrel export
   - **Test**: WorkerDetailPage Files tab still works after refactor (manual verification — no unit test framework for frontend)

4. [ ] Frontend: workspace tab on ProjectDetailPage
   - **Input**: `services/admin-frontend/src/pages/ProjectDetailPage.tsx`, workspace components from step 3
   - **Output**:
     - Add tab bar to ProjectDetailPage: "Overview" (current content) | "Workspace"
     - Workspace tab renders `WorkspaceBrowser` with `treeApiUrl=/wm-api/workspaces/{project_id}/tree` and `fileApiUrlPrefix=/wm-api/workspaces/{project_id}/files/`
     - Show "No workspace found" gracefully on 404
   - Update `services/admin-frontend/src/types/api.ts`: add `WorkspaceFileContentResponse` type (project_id instead of worker_id)
   - **Test**: Manual — navigate to project page, see Workspace tab, browse files

5. [ ] Frontend: WorkerDetailPage uses shared workspace component
   - **Input**: `services/admin-frontend/src/pages/WorkerDetailPage.tsx`, workspace components from step 3
   - **Output**:
     - Replace inline FilesTab with `WorkspaceBrowser` component
     - If worker has `project_id` → use workspace API urls (`/wm-api/workspaces/{project_id}/...`)
     - If no project_id → use worker API urls (`/wm-api/workers/{id}/...`) as fallback
     - Remove old inline TreeNode/TreeNodeView/buildTree/formatSize code from WorkerDetailPage
   - **Test**: Manual — worker detail Files tab still works, shows same files as project workspace tab

