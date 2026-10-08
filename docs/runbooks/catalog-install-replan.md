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

`replan` has two shapes on the same endpoint and request body. Sections 1-6 cover the
first: the install PR failed CI. Section 7 covers the second: a replanned cycle that was
planned as something other than the install and then stopped.

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

- The Architect logs `architect_job_started`, then `architect_replanned_install` with the
  package names and `architect_replanned_install_planned` with the version per package.
  It plans the reopen itself, without the LLM: exactly one new `install` Task per package
  of the cancelled install, created after `reopened_at` and pinning the release the
  catalog lists now, and no `fix` or other Task. The story moves to `in_progress`.
- If the story's brief was already admitted (`architect_planning_already_admitted`), the
  Architect logs `architect_replanned_install_owes_no_coverage`: admission happens once
  per brief, and a Task created outside a planning attempt is dispatch-admitted when it
  is created. Under a claimed, unadmitted brief it records coverage of every
  must-requirement by the new install Task and admits the plan.
- If a package is no longer installable, the Architect creates no Task and records a
  non-retriable planning failure `CatalogInstallRefused: <name>: <refusal>`, which parks
  the story for review.
- A deploy restart can leave the kit catalog briefly unreachable from the new container.
  The catalog read retries a transport failure for about 10 s. If it still fails, a
  story with an `install` Task in its history is not planned at all: the Architect logs
  `architect_planning_delayed_for_catalog`, creates no Task and records a retriable
  `KitCatalogUnavailable` planning failure. The scheduler re-sends the reopen after the
  planning backoff (`story_planning_retry_queued`), and that run plans the install.
  An unreachable catalog delays the plan; it does not change it.
- The cancelled install Task stays as history. It is not dispatched again and is outside
  the new work cycle.
- The scheduler dispatches the new install. The scaffolder creates `story/<story_id>`
  from `origin/main`, publishes it, and story completion opens a new PR. Watch that PR's
  CI. A second CI failure parks the story the same way, and this runbook applies again.
- If planning fails, the story shows a `planning` failure; follow the planning recovery
  in [returned-plan-recovery.md](returned-plan-recovery.md).

## 7. Second shape: the replanned cycle was planned as something else

Use this when a story that a first-shape `replan` reopened got something other than its
install in the new cycle, such as a `fix` Task and an engineering run, and someone
stopped it with `POST /api/stories/<story_id>/human-review`. Since this change the
Architect plans a replanned install itself (section 6), so this shape is for cycles
planned before it, or planned through a path that bypassed it.

Read the state first:

- `GET /api/stories/<story_id>`: `status` `waiting_human_review` and an
  `engineering_stop` with no `released_at`; note its `id`. `quarantine_reason` may be
  empty: the stop is released with whatever reason the story carries now.
- `GET /api/tasks/?story_id=<story_id>`: the cancelled `install` Task the first replan
  settled; note its `id` and `install_operation.id`. Its events
  (`GET /api/tasks/<id>/events`) carry the first replan's `note` with
  `catalog_install_settlement` and `operator_action` `replan`, written just before the
  story's current `reopened_at`.
- No run of the story is `queued` or `running`, no Task created since `reopened_at` is an
  `install` or `done`, and the remote `story/<story_id>` branch is absent. Clean the
  scaffolder workspace as in section 3 if a branch was ever created locally.

Then call the same endpoint on the cancelled install Task:

```http
POST /api/tasks/<cancelled_install_task_id>/catalog-install/recovery
Authorization: Bearer <admin-token>
Content-Type: application/json

{"operation_id": "<install_operation.id>", "action": "replan", "stop_id": "<engineering_stop.id>"}
```

On success the API answers 200 `settled` and, in one transaction, releases the stop,
cancels every Task of the current cycle that is not `done` or `cancelled` (an `in_dev`
fix goes straight to `cancelled`), adds a `note` on the install Task with the operation,
`operator_action` `replan` and the `cancelled_task_ids`, and moves the story
`waiting_human_review` → `failed` → `reopened`. After the commit it publishes the reopen
job; section 5 applies if it was lost. The Architect then plans the install as in
section 6.

Each unmet precondition is a 409 that changes nothing:

| Code | Meaning |
| --- | --- |
| `install_task_not_replanned` | The cancelled install Task carries no `replan` note |
| `story_reopened_since_replan` | The story's `reopened_at` is not the one that replan stamped |
| `stale_install_operation` | `operation_id` is not the Task's operation |
| `engineering_stop_mismatch` | `stop_id` is not the story's unreleased stop |
| `story_run_live` | A run of the story is `queued` or `running` |
| `cycle_has_install` | The current cycle already has an `install` Task |
| `cycle_has_done_task` | A Task of the current cycle is `done` |
| `install_branch_present` | The remote `story/<story_id>` branch still exists |
| `repository_unowned` | The repository is not a GitHub repository URL |

The second shape applies only while the story is `waiting_human_review`. On a cancelled
install Task of a story in any other status, the call is read as the first shape and
answers `install_task_not_done`; so does a repeated call after a success.

A replan note is matched to the reopen it caused by time: the note is written at the
start of the replan's transaction and `reopened_at` later in the same request, so they
are at most `REPLAN_REOPEN_WINDOW` (2 minutes) apart. A later reopen is further away.
