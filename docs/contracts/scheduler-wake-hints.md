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
- Targets: `story_completion` only.
- A hint carries routing only: no credentials, ids, correlation metadata or business state. It
  grants no permission to transition a Story, Task or Run and never invokes a business action.
- Producers publish with `shared.redis.scheduler_wake.publish_scheduler_wake` after the durable
  write the hint announces has committed. A publish failure loses only the hint.

## Ownership

| Target | Logical producer | Consumer |
|---|---|---|
| `story_completion` | api Task routes `POST /tasks/{id}/complete`, `POST /tasks/{id}/transition`, `DELETE /tasks/{id}`; langgraph engineering success settlement (`handle_engineering_success`) | scheduler-pipeline `story_completion` loop |

## Producers

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
  target.
- A Redis connect, subscribe or read failure, or an ended subscription, closes that Pub/Sub and
  Redis client and resubscribes after one `scheduler.dispatch_interval_seconds`. The loops keep
  their own interval deadlines and keep sweeping meanwhile.
- Cancellation propagates through the listener and the loop wait; each closes its Pub/Sub and
  owned Redis client once and leaves no helper task running.
