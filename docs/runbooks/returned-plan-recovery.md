# Recover a fully returned plan after deployment

For an admitted, confirmed Product Brief whose current attempt returned every
must-requirement and released no Tasks in that attempt, the existing operator action is
`POST /api/stories/{story_id}/retry-planning`. The API rechecks that evidence,
clears only the admission stamp, preserves confirmed content, and writes planning
as `retrying` due now. The scheduler publishes the Architect job; the next claim
replaces the attempt and its coverage. No new order or SQL update is needed.
Tasks from superseded attempts, including cancelled rows, do not count as work
in this attempt. Other parked planning failures still retry without resetting
the Brief's admission, including reopens whose original plan released work.

New admissions of this state already park in `waiting_human_review` with a typed
`planning_failed` reason. Older admissions need one data repair through the
supported `planning-outcome` endpoint before retry. Run these actions only after
the binding v2 planner fix has deployed. This card does not execute them.

## Production Story story-8c9a5af6

Use an authenticated admin Bearer token against the deployed API. Read:

- `GET /api/stories/story-8c9a5af6`: require project
  `6b7fa930-5c6f-405a-ba6d-3d17b836c8ee`.
- `GET /api/product-briefs/by-story/story-8c9a5af6`: require `confirmed_at`,
  `coverage_admitted_at` and `planning_attempt_id`.
- `GET /api/product-briefs/{brief_id}/coverage`: require a nonempty set of
  must-requirements, each with a current-attempt disposition, no `task_id`, and
  a nonempty `returned_reason`.
- `GET /api/tasks/?story_id=story-8c9a5af6`: require no Task whose
  `planning_attempt_id` equals the Brief's current `planning_attempt_id`.
  The original production refusal had zero Tasks; retained cancelled Tasks
  from superseded attempts also satisfy the current-attempt check.

If the Story is still `in_progress` with that old admitted refusal, perform the
one-off repair:

```http
POST /api/stories/story-8c9a5af6/planning-outcome
Authorization: Bearer <admin-token>
Content-Type: application/json

{
  "actor": "admin",
  "outcome": "failed",
  "retriable": false,
  "planning_attempt_id": "<current attempt from the brief>",
  "failure": {
    "code": "planning_failed",
    "source": "architect",
    "detail": "<requirement ids and returned_reason from current coverage>"
  }
}
```

The API bounds/redacts the detail and atomically parks planning with owner/admin
notices. Re-read the Story. If it is already `waiting_human_review` with a
`planning_failed` reason, skip this repair. The conditional repair is idempotent:
a repeated failure report for an already parked Story returns 409 and changes
nothing. Preserve any unrelated stop; this procedure applies only to the verified
all-returned planning refusal.

Then perform the admin action:

```http
POST /api/stories/story-8c9a5af6/retry-planning
Authorization: Bearer <admin-token>
Content-Type: application/json

{"actor": "admin"}
```

If the Story has an active `engineering_stop`, include its current `id` as
`stop_id` in that body. A missing or stale selection refuses release. This action
also performs the admission reset for the verified all-returned brief with no
current-attempt Tasks. Admission replay still answers `already_admitted`.
Check that the Story is `in_progress`, `planning.state` is `retrying`, and the
same Brief has `coverage_admitted_at: null` with unchanged confirmation/content.
There is no direct Redis publication. After a lost response, re-read before
retrying: a Story already `retrying` or replanned needs no further mutation.
Stop if the evidence differs, rather than clearing another plan's admission.
