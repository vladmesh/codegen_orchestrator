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
| `story_completion` | none yet: no business transition publishes a hint | scheduler-pipeline `story_completion` loop |

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
