# #1026 Admin UI: action buttons on entity pages

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Brainstorm bs-d124d343 Phase 4 — add action buttons to admin SPA that call the thin API endpoints from #1024 (done). Siblings #1020 (SystemConfig), #1023 (queue contracts), #1024 (API endpoints), #1025 (Settings page) are all done. No blockers remain.

**Current state**:
- Admin SPA: React 19 + React Router 7 + TanStack Query + Tailwind CSS
- Existing detail pages: Project, Task, Worker, Queue, User (no Story or Application detail pages)
- Task detail has Retry + Resume buttons; Worker detail has Kill button — both use the same inline confirmation pattern
- API endpoints ready: `send-to-architect`, `spawn-worker`, `stop`, `undeploy`, `redeploy`, `run-e2e`, `from-repo`, `DELETE secrets/{key}`
- No GET endpoint for reading secret keys (merge_secrets returns keys but only on POST)

**What needs to change**:
- ProjectDetailPage: add secrets editor (masked), Create Story form, Deploy from Repo form
- TaskDetailPage: add Spawn Worker button
- New StoryDetailPage: Send to Architect button
- New ApplicationDetailPage: Stop, Undeploy, Redeploy, Run E2E buttons
- New routes in App.tsx: `/stories/:id`, `/applications/:id`
- New API endpoint: `GET /projects/{id}/config/secrets/keys` to list secret key names for the editor
- Reusable ConfirmButton component to DRY up the confirmation pattern used across 6+ buttons

## Steps

1. [ ] Add `GET /projects/{id}/config/secrets/keys` API endpoint
   - **Input**: `services/api/src/routers/projects.py`
   - **Output**: New endpoint returning `{ keys: string[] }` — list of secret key names (no values exposed). Reads from project config, decrypts, returns sorted keys only.
   - **Test**: Service test in `services/api/tests/service/` — POST secrets, then GET keys, verify list matches

2. [ ] Create reusable `ConfirmButton` component
   - **Input**: Existing confirmation patterns in TaskDetailPage.tsx, WorkerDetailPage.tsx
   - **Output**: `services/admin-frontend/src/components/ui/ConfirmButton.tsx` — props: `label`, `confirmLabel`, `pendingLabel`, `confirmText`, `onConfirm`, `variant` (blue/red/green), `disabled`. Encapsulates the two-state (idle → confirming) pattern with Confirm/Cancel buttons.
   - **Test**: Visual — refactor existing Task Retry button to use ConfirmButton, verify no regression

3. [ ] Add Spawn Worker button to TaskDetailPage
   - **Input**: `services/admin-frontend/src/pages/TaskDetailPage.tsx`, endpoint `POST /tasks/{id}/spawn-worker`
   - **Output**: New "Spawn Worker" button in TaskActions, visible when task status is `backlog`, `todo`, or `failed`. Uses ConfirmButton. Calls `api.post('/tasks/{id}/spawn-worker', { actor: 'admin' })`. Invalidates task + events queries on success.
   - **Test**: Manual — navigate to task detail, verify button appears for correct statuses, click spawns worker

4. [ ] Create StoryDetailPage with Send to Architect button
   - **Input**: Existing page patterns (TaskDetailPage as template), endpoint `POST /stories/{id}/send-to-architect`
   - **Output**: New `StoryDetailPage.tsx` with: breadcrumb, status badge, metadata cards (type, priority, created_by), description section, "Send to Architect" ConfirmButton (visible when status is `created` or `reopened`). New route `/stories/:id` in App.tsx. Story links in ProjectDetailPage updated to use `<Link>`.
   - **Test**: Manual — navigate to story detail via project page, verify button calls endpoint and status transitions

5. [ ] Create ApplicationDetailPage with action buttons
   - **Input**: Application type in `types/api.ts`, endpoints: `stop`, `undeploy`, `redeploy`, `run-e2e`
   - **Output**: New `ApplicationDetailPage.tsx` with: breadcrumb, status badge, metadata cards (server, ports, health, SSL, uptime), 4 action buttons using ConfirmButton:
     - **Stop** (visible when `running`) — red variant
     - **Undeploy** (visible when `running`/`stopped`/`down`/`degraded`) — red variant
     - **Redeploy** (always visible) — blue variant
     - **Run E2E** (visible when `running`) — green variant
   New route `/applications/:id` in App.tsx. Application rows in ProjectDetailPage updated to link to detail page.
   - **Test**: Manual — navigate to application detail, verify buttons appear/hide based on status

6. [ ] Add secrets editor to ProjectDetailPage
   - **Input**: `ProjectDetailPage.tsx`, endpoints: `GET /projects/{id}/config/secrets/keys`, `POST /projects/{id}/config/secrets`, `DELETE /projects/{id}/config/secrets/{key}`
   - **Output**: New "Secrets" section on Overview tab — shows list of secret keys with masked "••••••" values. "Add Secret" button opens inline form (key + value inputs). Each key row has a Delete button (with ConfirmButton). Adding a secret calls merge_secrets POST, deleting calls DELETE.
   - **Test**: Manual — add a secret, see key appear, delete it, verify it's gone

7. [ ] Add Create Story form to ProjectDetailPage
   - **Input**: `ProjectDetailPage.tsx`, endpoint `POST /stories/`
   - **Output**: "Create Story" button in Stories section header. Clicking it expands an inline form: title (required), description (textarea), type (select: feature/bugfix/improvement). Submit calls `api.post('/stories/', { project_id, title, description, type })`. On success, invalidates stories query. Optional: "Create & Send to Architect" button that creates story then calls send-to-architect.
   - **Test**: Manual — create a story from project page, verify it appears in the list

8. [ ] Add Deploy from Repo form to ProjectDetailPage
   - **Input**: `ProjectDetailPage.tsx`, endpoint `POST /applications/from-repo`, servers list from `GET /servers/`
   - **Output**: "Deploy from Repo" button in Repositories section. Clicking it expands a form: repo URL (text), server (select dropdown populated from servers API), service name (text). Submit calls `api.post('/applications/from-repo', { repo_url, project_id, server_handle, service_name })`. On success, invalidates applications + repositories queries.
   - **Test**: Manual — fill out form, verify application is created and deploy is triggered

9. [ ] Integration test: full button coverage
   - **Input**: All pages modified in steps 3-8
   - **Output**: Service test covering: spawn-worker from task detail, send-to-architect from story detail, stop/undeploy/redeploy/run-e2e from application detail. These tests use the existing API test infrastructure (real DB + Redis).
   - **Test**: `make test-api-integration` passes

