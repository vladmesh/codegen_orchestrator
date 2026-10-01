# PR conflict repair contracts

Canonical invariants for story PR conflict detection, admission, repair attempts, settlement, and deliberate recovery.

Back to the [contracts index](../CONTRACTS.md).

## Story PR conflict repair

Conflict dispatch dispositions use the existing engineering admission lock ladder.
The following producers and consumers own each outcome; admission refusals create
no Run and a deferred paid gate result does not itself count an engineering attempt.

| Outcome | Authority and native consumer |
|---|---|
| Non-TODO/unadmitted Task, blocker, internal/legacy/draft project, workspace pending, roster change, stale cycle/PR or Story review | Admission refuses without Task/Story disposition; dispatcher waits for later eligibility. |
| Failed workspace ensure | Admission's native infrastructure park and workspace audit; infrastructure retry clears the exact park. |
| Sibling status/live Run, own live Run, finished current-iteration Run | Locked admission refuses busy work or returns a repair; dispatcher uses admitted-start or scoped terminal settlement, without buying another attempt. |
| Emergency stop, paid-count deferral, first/later budget denial | Paid gate audits the real decision; conflict admission commits Task/Story human review and both owed audiences together. This retains the existing engineering refusal consumer's human disposition, including the paid-count gate's deferred result. |
| Executor unavailable/confirmation required | Paid gate audits; admission commits the existing infrastructure park, even for deferred confirmation; scheduler consumes it and infrastructure retry owns recovery. |
| Admitted/lost-response start, operator spawn | Admission creates a real Run/hold; dispatcher publishes then uses locked admitted-start; operator spawn locks admission/start before publication. Generic conflict start remains refused. |
| Terminal/reclaimed/stuck/failed/finished-Run recovery | Consumer persists the real Run; scoped attempt-outcome owns retry/ending. Reclaim ACKs terminal work; native supervision or TODO recovery settles it. Infrastructure/resource/no-new-commit priorities remain. |
| Deliberate human recovery | Authenticated internal/admin Task resume checks the current conflict admission and matching stop, records the fresh iteration/bound, and returns Task/Story eligibility. Admission recognises only that native audited bound; prior decisions/Runs remain history. |

`EngineeringDispatchRead.refusal_disposition` names the parked conflict Task and
the immutable paid decision reference, never a Run FK. The scheduler consumes
the committed result without another start, Story stop or notice write. Lost
responses converge through the non-TODO fence and normal owed-notice delivery.
Other Task dispatch policy and the released dirty-Story restart boundary retain
their existing ownership.
The native resume status event alone may extend the bound; generic conflict
transition/reopen bodies cannot claim `action=operator_resume`, and client note
events have no status edges that could supply that authority.
An immutable no-Run stop continues to fence dispatch after mutable status or
metadata changes until that deliberate command supersedes it. Current conflict
admission and live-work checks also precede workspace-failure parks.

`POST /api/stories/{id}/repair-pr-conflicts` accepts `PRConflictRepairCommand`
and returns `PRConflictRepairRead`. The scheduler supplies the observed head;
the registered PO `reopen_story` tool may omit it only for the released
`waiting_human_review` / `github_app_merge_refused` / `dirty` quarantine.
The API resolves service, administrator or project-owner authority from credentials,
checks project, current PR and cycle (`reopened_at`, otherwise `created_at`), and
reads the open dirty PR, repository, story branch and actual default through the
GitHub App. Actor text and model-selected IDs grant no authority.

Task rows lock before Story rows. A deterministic Task ID per story cycle and
the Story lock serialize admission: one FIX Task, its immutable admission event
and `in_progress` landing commit together. Repeated requests reuse that Task;
neither repair admission nor CI retry starts another story cycle. Prior Tasks,
Runs and PR identities survive. The event records PR/head/default evidence and
the required `llm.task_default_max_iterations` bound, which normal engineering
dispatch enforces. No Run or queue message is created here; ordinary task
dispatch uses `/work-admission/engineering-dispatches`.

Once this Task completes, exhausts its failed-iteration retries, is cancelled or
requires human review, a still dirty PR exhausts repair rather than creating
another Task, even if its head changed. The native iteration ceiling permits
iteration zero followed by retries up to `max_iterations`; a failed attempt
below that ceiling remains eligible for the ordinary supervisor retry.
Exhaustion commits the named PR, Task and bound with both owed notice audiences.
An interrupted response is safe to repeat. Missing/stale evidence,
live unrelated work and unrelated quarantines fail without mutation. GitHub
read failures remain visible failures. This route never resets a task budget,
force-pushes, merges a dirty PR or replans the story.

`POST /api/stories/{id}/repair-pr-conflicts/attempt-outcome` accepts
`PRConflictRepairAttemptCommand` from an internal service or administrator and
returns `PRConflictRepairAttemptRead`. The command names the admitted Task,
failed engineering Run, its iteration, project, PR and immutable admission
cycle, plus `failed` or `gave_up`. Task, Story, Project and related Run locks
fence the decision in one transaction. Replaced cycles/PRs and older attempts
return `stale` without changing current work or notices; client reads grant no
settlement authority. Malformed or unrelated admission/Run evidence refuses.

The persisted terminal Run decides `failed` versus `gave_up` and supplies the
diagnostic; the command's disposition/detail are observations, not permission
to downgrade a refusal into a retry. Settlement accepts the native interruption
states `in_dev` after the Run write and before any Task write, and `todo` when
the worker finished before dispatch's status write. It commits the
Task disposition directly, without a generic Task-only human-review hop.
Task-only human review without a matching stop is not a supported repair state.

Engineering result delivery submits this command after persisting the Run.
Terminal queue reclaim ACKs that immutable Run without repeating the worker;
the normal stuck-task sweep discovers its still `in_dev` Task and submits the
same command. Failed-task supervision also submits it, after infrastructure and
resource dispositions, using immutable Run evidence even for a saved `gave_up`.
Dispatch's finished-Run recovery submits the command from `todo`; for a proven
infrastructure/resource refusal it restores `in_dev` discovery and defers to
the stuck sweep's existing priority routing. It never replays a repair refusal
as Task-only human review.
Request failure leaves the Task discoverable on the next native tick. Generic
terminal replay refuses failed conflict Tasks instead of bypassing settlement.

For an ordinary failed repair below the recorded ceiling, its next iteration
and `todo` state commit together, with an immutable per-attempt settlement event.
Lost responses and concurrent repeats reuse that event without another increment
or Run. No native repair producer splits retry into separate `backlog`/`todo`
writes; intermediate unreleased `backlog` retries are refused rather than
treated as supported history. Unrelated retry policy is unchanged.

At the ceiling or on `gave_up`, repair Task settlement, the named durable Story
stop and both owed notice audiences commit together. Replay reconciles the same
settlement without another stop/notice episode. Dirty-PR admission can already
have stopped an exhausted Task in the same cycle; the attempt command finishes
that matching stop without replacing its notice episode. The existing no-new-commit,
infrastructure and resource-wait dispositions retain priority. Owner/admin
delivery uses the existing owed-notification sweep.

Worker checkout fast-forwards a synchronized story tip to freshly fetched
default only when both local and fetched remote work are contained there.
Unmerged work stays; divergence and tracked dirty work fail visibly. Native
non-force publication and readback finish preparation before the turn baseline
is recorded, so advancing the base is not engineering output. Before a first
turn, both spawn and reclaim reconcile the Run with native prepared HEAD and
the worker's creation ownership. The Run retains the prepared worker/attempt
identity atomically with its baseline. A previous remote lookup alone cannot
survive this handoff as authoritative preparation evidence. A persisted turn
keeps its saved baseline on adoption, including after a lost response; it never
replaces that baseline with a post-agent head. Missing or inconsistent worker,
Run or preparation identity fails before another turn is published. Ordinary
later attempts on a reused worker retain their own pre-turn baseline rather
than the checkout baseline of the worker's earlier creator attempt.

`services/api/src/dependencies.py` and `services/api/src/routers/_recipients.py`
resolve the caller once. An LK bearer acts only as the token subject. An
internal key is a service principal and may name an actor through
`X-Telegram-ID`; a bare Telegram header is not authentication. Authorization,
project ownership, allocation administration, and recipient resolution use that
principal rather than an untrusted header lookup.
