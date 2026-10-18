# Scheduler wake hints

A wake hint asks one scheduler loop to run its next cycle now instead of at the end of its
interval. It is an optimisation of latency only: the loop's interval sweep stays the recovery
path, and the awakened cycle decides from the same durable Story/Task/Run state it always reads.

## Wire contract

- Transport: native Redis Pub/Sub (`PUBLISH`/`SUBSCRIBE`), channel `scheduler:wake`. It is not a
  Redis Stream: nothing is stored, acknowledged, retried or replayed.
- Message: the JSON body of `SchedulerWakeHint` in `shared/contracts/queues/scheduler_wake.py`,
  for example `{"target":"story_completion"}`. `target` is a `SchedulerWakeTarget`; no other
  field is accepted.
- Targets: `story_completion`, `task_dispatcher`, `lifecycle_supervision`, `temporary_access`,
  `qa_routing` and `pr_ci`; each wakes only the loop of that name.
- A hint carries routing only: no credentials, ids, correlation metadata or business state. It
  grants no permission to transition a Story, Task or Run and never invokes a business action.
- Producers publish with `shared.redis.scheduler_wake.publish_scheduler_wake` after the durable
  write the hint announces has committed. A publish failure loses only the hint.

## Ownership

| Target | Logical producer | Consumer |
|---|---|---|
| `story_completion` | api Task routes `POST /tasks/{id}/complete`, `POST /tasks/{id}/transition`, `DELETE /tasks/{id}`; langgraph engineering success settlement (`handle_engineering_success`) | scheduler-pipeline `story_completion` loop |
| `task_dispatcher` | api Product Brief admission `POST /product-briefs/{id}/admit` | scheduler-pipeline `task_dispatcher` loop |
| `lifecycle_supervision` | api Run update `PATCH /runs/{id}` on the first typed success of an ordinary story deploy | scheduler-pipeline `lifecycle_supervision` loop |
| `temporary_access` | api Run update `PATCH /runs/{id}` on the first readback proof of a temporary-access grant or revoke operation, and on a QA Run's first terminal state | scheduler-pipeline `temporary_access` loop |
| `qa_routing` | api Run update `PATCH /runs/{id}` on a story-scoped QA Run's first terminal state | scheduler-pipeline `qa_routing` loop |
| `pr_ci` | api Story transition `POST /stories/{id}/pr_review` | scheduler-pipeline `pr_ci` loop |

## Producers

### `story_completion`

- The api Task routes publish one `story_completion` hint when the request moves a story-scoped Task
  to `done` or `cancelled`: `complete`, a `transition` to either status (the install cancellation
  branch included) and `DELETE`. The route publishes on the initialized Redis client right after its
  own `db.commit()` returns and before the response refresh. A storyless Task, an intermediate hop,
  a refused request, a failed commit and an already-cancelled no-op publish nothing. The shared
  `apply_task_completion` and `apply_cancellation` never publish, because their callers own the
  transaction.
- Liveness replay of a finished Run and success settlement with a planning Task reach `done`
  through the `transition` route, so its hint covers them; success settlement still publishes its
  own hint after its later writes. The two hints coalesce in the loop and either one alone is
  enough, because the sweep reads only durable Story, Task and Run state.
- Engineering success settlement publishes one `story_completion` hint for a story-scoped attempt
  with a commit, on the consumer's connected Redis client. It publishes after the terminal Run
  `completed` write returns and, with a planning Task, after the `in_ci`, `testing`, `done`
  transitions and the `iteration_end` event write return; callbacks and the direct deploy follow it.
- The Task transitions and event write stay best-effort: after one fails the hint is still
  published, and the sweep acts only on what committed. A storyless success, a failed terminal Run
  write and every rejected, failed or gave-up outcome publish nothing.
- A Redis or socket error on publish is logged as `story_completion_wake_failed` with the story id,
  the task id on the api, and the error type only; the committed response or settlement result,
  callbacks, deploy and durable data are unchanged. Cancellation propagates. There is no retry.
- Every other write that can make a Story eligible still waits for the interval: dispatch admission
  completing a finished Run, catalog install publication and recovery, commit recovery, PR conflict
  repair settlement, non-success deploy-fix settlement, the Product Brief takeover cancellation and
  every other Task mutation outside the routes above.

### `task_dispatcher`

- Product Brief admission publishes one `task_dispatcher` hint when its transaction released a
  nonempty set of Tasks: on the initialized Redis client, right after its own `db.commit()`
  returns and before the response refresh. An `already_admitted` replay, an `incomplete` answer, a
  refused request, a failed commit and an admission that released no Task (the fully returned
  taskless park included) publish nothing. Coverage writes, confirmation and claims never publish.
- The awakened cycle runs the unchanged `dispatch_todo_tasks` then catalog install recovery, so
  the API dispatch admission point still decides every released Task; the hint admits nothing.
- A Redis or socket error on publish is logged as `task_dispatcher_wake_failed` with the brief
  id, the story id and the error type only; the committed admission and its response are
  unchanged. Cancellation propagates. There is no retry.
- Every other write that can make a Task dispatchable still waits for the interval: Task creation
  in `todo`, operator resume and retry, a blocker reaching `done`, a Story start or an engineering
  stop release, a freed paid-work slot, and the catalog install and repair paths.

### `lifecycle_supervision`

- `PATCH /runs/{id}` publishes one `lifecycle_supervision` hint when the request gives a
  story-scoped `deploy` Run its first terminal typed success: the locked row before the update
  lacks, and the final row holds, a terminal status with a result that validates as
  `DeployRunResult` with `deploy_outcome` `success`, the proof the deploy supervisor routes to its
  QA handoff. A status and result written in two requests publish on the second, and a late
  success on a cancelled deploy without a result publishes too.
- The route publishes on the initialized Redis client right after its own `db.commit()` returns
  and before the response refresh. An identical terminal replay, a metadata-only update, an
  intermediate status, a terminal write without a result, a non-success or malformed result,
  another Run type, a storyless Run, a refused request and a failed commit publish nothing. The
  classification never refuses or fails a write.
- A temporary-access grant or revoke Run never publishes this hint: `temporary_access_grant_id` or
  `temporary_access_operation` as a key of the stored or the final metadata, with a null value or
  cleared by the update, names the `temporary_access` loop as the owner of its proof.
- The langgraph deploy consumer records its success through this route before its callbacks, so
  this hint covers it; it publishes no hint of its own.
- The awakened cycle runs the unchanged `supervise_lifecycle_once` sweeps in their order with
  per-sweep error isolation; the deploy supervisor still decides the handoff from the durable
  Story and its latest deploy Run, and the hint hands off nothing.
- A Redis or socket error on publish is logged as `lifecycle_supervision_wake_failed` with the run
  id, the story id and the error type only; the committed Run and its response are unchanged.
  Cancellation propagates. There is no retry.
- Every other write that lifecycle supervision acts on still waits for the interval: a
  non-success deploy outcome, a queued deploy handoff awaiting recovery, a stuck, failed or
  resource-waiting Task, an awaited user secret, and every recovery, direct-database, install,
  repair or takeover write.

### `temporary_access`

- `PATCH /runs/{id}` publishes one `temporary_access` hint when the request gives a temporary-access
  operation Run its first readback proof: the locked row before the update lacks, and the final row
  holds, `deploy` type, status `completed` and a result that validates as `DeployRunResult` with
  `deploy_outcome` `success` and no `skipped_reason`, the proof the sweep and the grant transitions
  accept. A status and result written in two requests publish on the second. A `story_id` is not
  required; the operation Runs the sweep persists are storyless.
- Both rows must carry the same marker pair: `temporary_access_grant_id` a nonempty string and
  `temporary_access_operation` exactly `grant` or `revoke`. A missing, null or malformed key, and a
  pair the update adds, rebinds, changes or clears, publishes nothing.
- The route publishes on the initialized Redis client right after its own `db.commit()` returns
  and before the response refresh. An identical terminal replay, a metadata-only update, an
  intermediate, failed or cancelled status, a terminal write without a result, a skipped, non-success
  or malformed result, another Run type, an ordinary deploy, a refused request and a failed commit
  publish nothing. The classification reads only the Run row: it never reads or locks the grant,
  and never refuses or fails a write.
- The langgraph deploy consumer records an operation's proof through this route, so this hint
  covers it; it publishes no hint of its own.
- A QA Run's first terminal state publishes this hint too, with or without a story, as
  [QA terminal state](#qa-terminal-state) describes.
- The awakened cycle runs the unchanged `supervise_temporary_access` sweep. It still reads each
  grant's current operation Run, expiry and QA state, so a proof of a superseded operation settles
  nothing; the hint grants, revokes and releases nothing.
- A Redis or socket error on publish is logged as `temporary_access_wake_failed` with the run id,
  the story id and the error type only; the committed Run and its response are unchanged.
  Cancellation propagates. There is no retry.
- Every other write the sweep acts on still waits for the interval: grant creation, a failed,
  cancelled, skipped, missing or stale operation, an expired grant, a `revoke_failed` record
  awaiting retry or escalation, the QA routing transition an escalation waits for, and every drain
  or recovery write.

### QA terminal state

- `PATCH /runs/{id}` publishes when the request gives a `qa` Run its first terminal state: the
  locked row before the update is `queued` or `running`, and the final row is `completed`, `failed`
  or `cancelled`. It publishes `qa_routing` when the Run has a `story_id`, then `temporary_access`
  whether or not it has one. A storyless QA Run has no story to route, but its grant still owes
  cleanup.
- The hints announce a committed terminal state, not a valid verdict: a cancellation without a
  result publishes both, and the result's shape is never read. A result written while the Run is
  still running publishes nothing until a later request makes the Run terminal.
- The route publishes on the initialized Redis client right after its own `db.commit()` returns
  and before the response refresh. A terminal replay, a metadata- or accounting-only update, a
  nonterminal update, another Run type, a refused request (a late verdict on a recorded QA outcome
  included) and a failed commit publish nothing. The classification reads only the Run row: it
  never reads the Story or the grant, never touches `qa_routed_at`, and never refuses or fails a
  write.
- The langgraph QA consumer and every other terminal writer of a QA Run record it through this
  route, so these hints cover them; none publishes a hint of its own.
- The awakened `qa_routing` cycle runs the unchanged `supervise_testing_stories` sweep, which reads
  each TESTING story's latest QA Run and routes its verdict; only that story transition writes
  `qa_routed_at`. The awakened `temporary_access` cycle starts the grant's cleanup on its own. The
  two loops keep no order between them; an exhausted cleanup's escalation still waits for
  `qa_routed_at`.
- Each target is published on its own: a Redis or socket error is logged as
  `qa_routing_wake_failed` or `temporary_access_wake_failed` with the run id, the story id and the
  error type only, and the other target is still attempted. The committed Run and its response are
  unchanged. Cancellation propagates and skips any remaining target. There is no retry.
- Every other write QA routing acts on still waits for the interval: a story reaching TESTING after
  its QA Run turned terminal, a queued QA handoff awaiting recovery, an invalid stored result, and
  every recovery, operator or direct-database write.

### `pr_ci`

- `POST /stories/{id}/pr_review` publishes one `pr_ci` hint for every committed entry into
  `pr_review`, a later work cycle's entry included, on the initialized Redis client right after
  its own `db.commit()` returns and before the response refresh. The transition rules refuse a
  repeat from `pr_review` and every other invalid source status, so a refused request, a replay and
  a failed commit publish nothing. No other Story update or transition publishes it.
- The sender reads no GitHub state and no record beyond the locked Story. Story completion stores
  the exact PR number before it calls this route, so the hint also covers a PR whose CI finished
  before the entry committed.
- The awakened cycle runs the unchanged `poll_pr_ci_once`: `poll_merged_prs` then
  `poll_ci_failures`, each with its own error isolation. They still read the current PR, checks and
  images from GitHub against the stored PR number and story branch, and own every merge, refusal,
  deploy and CI-fix decision; the hint merges, deploys and routes nothing.
- A Redis or socket error on publish is logged as `pr_ci_wake_failed` with the story id and the
  error type only; the committed transition and its response are unchanged. Cancellation
  propagates. There is no retry.
- Every GitHub-side change still waits for the interval: CI completing with success or failure, a
  PR becoming mergeable, behind or conflicted, a merge, and the published images of a merge
  commit. No authenticated ingress for GitHub events exists, so this loop receives no hint from
  GitHub.

## Listener

scheduler-pipeline runs one `scheduler_wake` listener beside its loops under the same
supervisor. It must hold an event for every target in the vocabulary, or it refuses to start.

## Delivery and coalescing

- Delivery is lossy: a hint published while no listener is subscribed, during a reconnect, or
  before a restart is gone. The loop's `scheduler.dispatch_interval_seconds` sweep covers it.
- A hint sets the target loop's `asyncio.Event`. The loop clears the event when a cycle starts,
  so any number of hints during a cycle, or between its end and the next wait, yield exactly one
  prompt follow-up cycle. Cycles never overlap and hints queue no work or tasks.
- After a cycle the loop waits for its event or its interval, whichever comes first; the
  interval is reread from configuration for every wait. There is no minimum delay on the wake
  path.

## Rejection and failure

- A payload that is not text, exceeds 256 characters, is not JSON, names an unknown target or
  carries an extra field is rejected and logged as `scheduler_wake_hint_rejected` with a reason
  and validation shape only, never the payload. It wakes nothing and is never mapped to another
  target. A listener older than a target rejects it the same way, and the interval sweep covers it.
- A Redis connect, subscribe or read failure, or an ended subscription, closes that Pub/Sub and
  Redis client and resubscribes after one `scheduler.dispatch_interval_seconds`. The loops keep
  their own interval deadlines and keep sweeping meanwhile.
- Cancellation propagates through the listener and the loop wait; each closes its Pub/Sub and
  owned Redis client once and leaves no helper task running.
