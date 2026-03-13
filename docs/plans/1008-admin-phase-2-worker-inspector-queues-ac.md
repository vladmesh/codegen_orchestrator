# #1008 Admin Phase 2 — worker inspector + queues + action buttons

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

Task #1008: Admin Phase 2 — worker inspector + queues + action buttons.

The admin-frontend SPA already has scaffold pages (Dashboard, Projects, Tasks, Queues, Logs) and two placeholders: WorkersPage and ServersPage. The worker-manager introspection API (#1007) is complete — 7 endpoints at `/wm-api/*` (list, detail, logs, tree, files, prompts, kill). The API service has `/debug/queues` (bindings-based response), `/tasks/{id}/resume`, and `/tasks/{id}/transition` endpoints.

Current gaps:
- WorkersPage is a placeholder
- QueuesPage works but uses a flat `QueueHealth` type that doesn't match the actual `{status, bindings[], issues[]}` response — it works by accident (iterating `Object.entries` on the top-level keys). Needs proper typing + enhanced UI with issues display.
- `api.raw()` only supports GET — need raw variants for DELETE and POST (kill worker, resume/retry task)
- No TypeScript types for worker-manager API responses
- No WorkerDetailPage with tabs (logs, prompts, file browser)

## Steps

1. [ ] Extend API client + add worker types
   - **Input**: `src/lib/api.ts`, `src/types/api.ts`
   - **Output**: `api.rawDelete()`, `api.rawPost()` methods; TypeScript interfaces for WorkerSummary, WorkerDetail, WorkerLogsResponse, FileTreeEntry, FileContentResponse, PromptsResponse, DebugQueuesResponse (with bindings[], issues[], status)
   - **Test**: type-check passes (`npx tsc --noEmit`)

2. [ ] Workers list page — replace placeholder
   - **Input**: `src/pages/WorkersPage.tsx`
   - **Output**: Table/card grid of workers from `GET /wm-api/workers/`. Show id, status (with StatusBadge), project_id (link to project), workspace_path, last_activity, error. Auto-refresh every 5s. Link each worker to `/workers/:id`.
   - **Test**: page renders with loading state, empty state, populated state (manual browser check + tsc)

3. [ ] Worker detail page — header + tabs skeleton
   - **Input**: new `src/pages/WorkerDetailPage.tsx`, update `src/App.tsx` routes
   - **Output**: Route `/workers/:id`. Header with worker id, status badge, kill button. Tab navigation: Console | Prompts | Files. Fetches `GET /wm-api/workers/:id` for detail. Kill button calls `DELETE /wm-api/workers/:id` with confirmation dialog.
   - **Test**: page loads worker detail, kill button shows confirm, tsc passes

4. [ ] Worker detail — Console tab (logs)
   - **Input**: `WorkerDetailPage.tsx` or extracted component
   - **Output**: Fetches `GET /wm-api/workers/:id/logs?tail=200`. Displays in monospace scrollable container (dark bg, light text). Auto-refresh every 5s. Tail selector (100/200/500/1000).
   - **Test**: logs render in pre/code block, tail param changes refetch

5. [ ] Worker detail — Prompts tab
   - **Input**: `WorkerDetailPage.tsx` or extracted component
   - **Output**: Fetches `GET /wm-api/workers/:id/prompts`. Shows CLAUDE.md and TASK.md in separate cards with monospace pre-formatted text. Shows "not found" if null.
   - **Test**: both prompts render, null prompts show fallback

6. [ ] Worker detail — Files tab (tree + viewer)
   - **Input**: `WorkerDetailPage.tsx` or extracted component
   - **Output**: Left panel: file tree from `GET /wm-api/workers/:id/tree` (collapsible directories). Right panel: file content from `GET /wm-api/workers/:id/files/{path}` with syntax-highlighted monospace display. Click file in tree → loads content.
   - **Test**: tree renders, clicking file loads content panel, directories collapse/expand

7. [ ] Upgrade QueuesPage — proper types + enhanced UI
   - **Input**: `src/pages/QueuesPage.tsx`, `src/types/api.ts`
   - **Output**: Use DebugQueuesResponse type. Show status indicator (ok/degraded). Display bindings as cards with stream name, group, description, length, pending (yellow if >0), consumers, last_delivered_id. Show issues list (if any) as warning banner at top.
   - **Test**: page renders with real data shape, issues banner appears when present

8. [ ] Action buttons — retry failed task, resume blocked task
   - **Input**: `src/pages/TaskDetailPage.tsx`
   - **Output**: Conditional action buttons on task detail: "Retry" (for status=failed → transitions to backlog via `POST /api/tasks/:id/transition?to_status=backlog`), "Resume" (for status=waiting_human_review → calls `POST /api/tasks/:id/resume` with guidance text input). Both use useMutation + invalidate query cache. Confirmation before action.
   - **Test**: buttons appear only for correct statuses, mutations fire correct endpoints

9. [ ] Sidebar polish + final wiring
   - **Input**: `src/components/layout/Sidebar.tsx`
   - **Output**: Remove "soon" badges from Workers. Ensure all navigation links are active. Verify all routes work end-to-end.
   - **Test**: full click-through of Workers list → detail → each tab, Queue page, Task actions

