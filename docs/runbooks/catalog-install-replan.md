# Re-plan a catalog install whose PR failed CI

A story whose catalog-install PR failed CI is parked by the scheduler in
`waiting_human_review` with an unreleased engineering stop and the reason
`Catalog installation requires review: PR CI failed at <sha>; review <url>. No automatic
engineering repair.` The install Task is `done` and its `install_operation` is `published`.

Re-running that Task cannot help: it pinned the package release when it was planned, and
the scaffolder preflight refuses a catalog that has moved on. The fix is usually a newer
package release, so the story needs a fresh install plan from the Architect. The
operator action for that is `replan` on the existing catalog-install recovery endpoint.
It writes nothing to GitHub. The operator does the GitHub cleanup first.

## 1. Read the parked state

Use an authenticated admin Bearer token against the deployed API.

- `GET /api/stories/<story_id>`: require `status` `waiting_human_review`, a
  `quarantine_reason` with `source` `scheduler` whose `detail` starts with
  `Catalog installation requires review:`, and an `engineering_stop` with no
  `released_at`. Note `engineering_stop.id` and the PR URL in the detail.
- `GET /api/tasks/?story_id=<story_id>`: find the `install` Task with `status` `done`.
  Note its `id` and `install_operation.id`; `install_operation.state` must be
  `published`, and `install_operation.cycle_started_at` must equal the story's
  `reopened_at`, or its `created_at` if it was never reopened.

## 2. Close the PR and delete the remote branch

Close the install PR named in the stop detail without merging it, then delete the
remote branch `story/<story_id>` in the product repository. The next install starts
from `origin/main` only when that remote branch is absent. While it exists, `replan`
answers 409 `install_branch_present` and changes nothing.

## 3. Clean the retained scaffolder workspace

The scaffolder keeps the product checkout at `<WORKSPACE_BASE_PATH>/<repository_id>`
inside its container. The next install refuses a dirty tree, Copier `.rej`/`.orig`
leftovers, and a local `story/<story_id>` branch that the remote no longer has. In that
checkout:

1. Check that no install is running for the project (no install operation in `running`).
2. `git status --porcelain --untracked-files=all` must print nothing. Remove what the
   failed attempt left behind; do not keep it, the branch is being discarded.
3. `git fetch --prune origin`, then `git ls-remote --heads origin story/<story_id>` must
   print nothing.
4. `git switch --detach origin/main` and `git branch -D story/<story_id>`, so no local
   story branch is left. `git status --porcelain --untracked-files=all` still prints
   nothing.

## 4. Replan

```http
POST /api/tasks/<install_task_id>/catalog-install/recovery
Authorization: Bearer <admin-token>
Content-Type: application/json

{"operation_id": "<install_operation.id>", "action": "replan", "stop_id": "<engineering_stop.id>"}
```

On success the API answers 200 with `outcome` `settled`, and in one transaction it:
releases the stop, records a note event with the settled operation on the install Task,
cancels that Task (`done` → `backlog` → `cancelled`), and moves the story
`waiting_human_review` → `failed` → `reopened` with a new `reopened_at` and no quarantine
reason. After the commit it publishes a reopen job to the Architect.

Each precondition that does not hold is a 409 that changes nothing:

| Code | Meaning |
| --- | --- |
| `install_operation_missing` | The Task is not an install with an operation |
| `stale_install_operation` | `operation_id` is not the Task's operation |
| `install_operation_not_published` | The operation is not `published` |
| `install_task_not_done` | The install Task is not `done` (also a repeated `replan`) |
| `story_not_waiting_human_review` | The story is not parked for review |
| `unrelated_story_stop` | The park is not the scheduler's install-CI review |
| `engineering_stop_mismatch` | `stop_id` is not the story's unreleased stop |
| `stale_install_cycle` | The operation belongs to an older work cycle |
| `install_branch_present` | The remote `story/<story_id>` branch still exists |
| `repository_unowned` | The repository is not a GitHub repository URL |

A repeated call after a success answers 409 `install_task_not_done` and publishes nothing.

## 5. If the Architect job was not published

If the answer carries `reason` `architect_publish_failed`, or the API logged
`catalog_install_replan_publish_failed`, the database changes are committed but the
Architect was never asked. Re-send it through the durable handoff:

```http
POST /api/stories/<story_id>/send-to-architect
Authorization: Bearer <admin-token>
Content-Type: application/json

{"actor": "admin"}
```

It moves the reopened story to `in_progress` and records the planning it owes. The
scheduler then publishes the reopen job. Use it only while the story is still `reopened`
and no new Task has appeared for it.

## 6. What to watch next

- The Architect logs `architect_job_started` for the story, then plans. A new `install`
  Task created after `reopened_at` should pin the current catalog release, and the story
  should move to `in_progress`.
- The cancelled install Task stays as history. It is not dispatched again and is outside
  the new work cycle.
- The scheduler dispatches the new install. The scaffolder creates `story/<story_id>`
  from `origin/main`, publishes it, and story completion opens a new PR. Watch that PR's
  CI. A second CI failure parks the story the same way, and this runbook applies again.
- If planning fails, the story shows a `planning` failure; follow the planning recovery
  in [returned-plan-recovery.md](returned-plan-recovery.md).
