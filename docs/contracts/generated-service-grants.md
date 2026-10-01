# Generated-service grants and deploy redaction

Canonical contracts for generated-service user grants and credential-safe deploy diagnostics.

Back to the [contracts index](../CONTRACTS.md).

## Generated-service user grants

Generated Telegram services use their `USERS_GRANT_CAPABILITY` generated secret
only through the deploy resolver's in-memory `secret_values`. `users_grant_intents`
is the durable, typed non-secret record. Its identity is `(kind, project, verified
channel/external identity)`, not a deploy Run id. The API lifecycle operation is
the sole route that creates, finds, binds or rebinds an intent and dispatches a
permanent grant attempt. Every deploy Run it creates is one immutable execution
attempt and holds only the intent reference plus its exact SHA.

An APPLIED intent wins redelivery and is never rewritten or dispatched again. An
automatic source Run cannot replace a binding while its execution Run is live,
cannot reopen an exhausted target, and cannot bind a SHA retained in
`target_history`; it receives `in_flight`, `exhausted`, or `stale_target` with no
new Run. Only after the prior execution is terminal may a genuinely new
authoritative target record the replaced application/deployment/SHA and its
closed admission count, reset the target-epoch counter, and dispatch one bounded
new epoch. It never redeploys a superseded SHA. Initial-owner seeding finds the
single durable intent while every PR poll, QA cycle, story fix and supervisor
recovery keeps its own Run id. Retries after ordinary deploy, infrastructure,
secret, or post-health grant failure reacquire that seed through the same
lifecycle operation.

`GrantIntentLifecycleResult` is the per-call boundary for that operation. Only
`dispatched` carries the newly minted Run id and its immutable target;
`already_applied`, `in_flight`, `stale_target`, and terminal `exhausted` carry
neither, even though the durable intent retains its safe execution history. Before a fresh
`dispatched` admission, the lifecycle locks and evaluates the same
`deploy.max_deploy_retries` control as the scheduler. The admission counter is
bounded for one target epoch, not for the lifetime intent. Once the number of
immutable Runs in that epoch reaches the ceiling, it records and commits the
safe terminal `failed` outcome before returning `exhausted`, without creating or
publishing a `(max + 1)` Run. A fresh explicit add-user or ownership-transfer
request may reopen an exhausted same-target intent in a new user-directed epoch;
it retains the same row and `retry_history`. Automatic supervisor, PR-poller,
infrastructure, and waiting-secret recovery cannot reopen that epoch. Applied
and in-flight responses consume no attempt. PR polling and every
supervisor recovery route use that disposition rather than an intent's
historical execution id. A lost completion response reconciles the source deploy
through the ordinary successful-deploy handoff without spending a retry;
infrastructure and user-secret recovery only claim an intent redispatch when the
result is `dispatched`. INITIAL_OWNER exhaustion commits the matching current Story's
typed failure and both owed notice audiences in API admission; scheduler consumers
must not issue a second stop or best-effort alert. A stale callback cannot stop new work.
At a zero ceiling, the poller's persisted current-cycle merged PR, CI and image
observation let API admission stop the matching PR-review Story without a Run;
stale PRs, unrelated stops and live work grant no such transition. A terminal
cancelled deploy is an exhausted source only with its validated typed
`DeployRunResult.deploy_outcome=cancelled`; a resultless superseded cancellation
does not supply that evidence.

`GET /api/projects/{id}/users/initial-owner-deployment` and the existing intent
read authorize the credential-derived owner/admin or internal reader and expose
`GrantIntent.exhaustion`. `GrantIntentLifecycleResult.exhaustion` has the same
`GrantIntentExhaustion`: typed `initial_owner_deployment_exhausted`, closed count,
immutable target, and any verified exhausted execution Run. The locked API
lifecycle checks the current admitted Run, owner, Story and epoch before exposing
`retry_initial_owner_deployment` with
`GrantIntentRetryCommand.expected_execution_run_id` only while the effective
locked `deploy.max_deploy_retries` ceiling is positive. Zero admission has no
exhausted Run, action or command, including after a policy increase. A real
exhausted Run retains its history when policy becomes zero, but current API and
PO readbacks offer no action or command. A later positive policy restores only
a still-fenced offer; it never resets the intent automatically. Story failure
and both owed notice texts are stable historical explanations that direct readers
to authenticated current readback, without promising an executable command.
This readback never claims a new dispatch.

`POST /api/projects/{id}/users/grant-intents/{intent_id}/retry` accepts that command
only from the current credential-derived project owner or administrator. The
established internal service-on-behalf-of-verified-user transport may carry the
PO caller; a service alone, bare Telegram header, actor string or lifecycle JSON
flag cannot grant reset authority. The registered PO read/retry tools carry server
context and the observed fence unchanged across response loss. Queued means admitted,
not deployed or active; deployment credentials remain platform provisioned.

Under project/intent/Story/Run locks, applied and live work win. Same-target recovery
requires the current verified identity and immutable source Run, exact distinct
head/built SHAs, current closed merged PR and Story cycle, matching native epoch
admissions, no other live work and the corresponding deployment exhaustion stop.
Released INITIAL_OWNER Runs without epoch stamps and bare failed Stories remain
supported only with genuine matching native admissions/PR/cycle evidence and a
failed landing after the source Run. Free text alone grants nothing. Missing,
replaced or unrelated evidence refuses before mutation. Archived Stories/projects
and unrelated quarantines remain protected.

One transaction appends the authenticated actor, prior count/target, command fence
and prior stop/notice facts to `retry_history`, resets that epoch, returns its matching
Story to `deploying` without a new cycle, and creates its one immutable deploy Run.
Prior executions and targets remain history. Repeated commands, including an old
command replayed after the new epoch exhausts, cannot reopen it. A policy or secret
fix alone cannot reset committed exhaustion; repeated inactive completion cannot
downgrade it. The new epoch uses the current configured `deploy.max_deploy_retries`;
zero refuses before any reset, Run or Story change. A policy update between GET
and POST therefore returns a typed non-actionable exhaustion. Existing
PUBLISH_OWED recovery publishes the same real Run
and reports `in_flight`, not a newly dispatched execution. Publication and notices
retain their established at-least-once transport semantics.
The deploying supervisor discovers an owed queued initial-owner Run after the
handoff grace interval and invokes the same locked lifecycle with
`GrantIntentLifecycleRequest.expected_execution_run_id`. The API requires that
exact current queued Run, intent, Story and target before publication; the
request cannot create an intent or admit another Run. A human command against
`retryable` with remaining admissions refuses with 409 and preserves its epoch.

`StoryFailureCode.INITIAL_OWNER_DEPLOYMENT_EXHAUSTED`, source `api`, records only
bounded native intent/attempt/target/count identifiers. Its durable wording
explains exhaustion and directs the owner/PO to authenticated current readback;
zero admission also states that no Run was admitted and same-target retry is
unavailable for that epoch. It never embeds a policy-sensitive action.
The API stores it with the matching Story stop and owner/admin owed obligations.
Generic Story stop bodies cannot claim this API-owned grant exhaustion code.
Response loss converges without replacing that notice episode. History and PO
readback explain exhaustion without requesting customer-supplied DEPLOY_* data.

`GrantIntentLifecycleRequest` (`shared/contracts/dto/users_grant.py`) adds an optional
`merged_pr_number` for the PR poller. It selects evidence, never grants a reset. After its
GitHub App reads the current story PR and observes successful image publication on `main`
for the built merge SHA, the poller persists `generated_product_timeline.deploy_observation`
(story, project, primary repository URL, observation time), alongside the exact PR and CI
reading, before admission. This observation can be written only by an internal service;
ordinary authenticated story updates keep their existing authorization.

Under the existing project/intent locks, APPLIED and a live execution take precedence.
A superseded SHA is refused. Any automatic replacement requires the selected current PR,
matching story/project/repository, exact head and built SHAs, one completed successful CI run
on `main`, and merge/observation timestamps within the current story cycle. Missing or
inconsistent evidence returns a visible 409 without mutation. A changed SHA without merge
authority returns `stale_target`, or `exhausted` if the current epoch already exhausted.
A verified new merged target may replace even a committed exhausted epoch on the same
intent: it retains the old target/count, resets only the new target counter and creates
one immutable Run under the same ceiling. An exhausted same target remains terminal
to automatic callers.
Generic supervisor, infrastructure and secret recovery supply no merge authority. Publication
reacquires the existing project/intent locks after the Run commit, serializing concurrent
owed dispatches without minting another Run or changing prior execution history.

After smoke success, deploy grants through `POST /users/grant` and requires
`GET /users/access` to report that exact identity active before the API records
APPLIED or reports access live. An incoming ownership transfer changes
`Project.owner_id` only in that readback completion transaction. The capability
is never an override, project configuration value, URL, event field, or log value.
QA temporary-access grant or revoke remains ineligible for these permanent
intents, even when it carries a deployment SHA.

Temporary QA access uses the same generated-service capability but a distinct
durable record. It binds the central QA Telegram identity, application, base URL,
and SHA before dispatch. Grant and revoke each require the matching active or
inactive `/users/access` readback; the capability remains in deploy-local memory.
The reconciler retries only the record's immutable target. Missing or stale grant
and revoke operation Runs consume their separately recorded, bounded attempt
budgets. A revoke that exceeds its attempt or unrevoked-time bound receives one
persisted administrator escalation and never releases or republishes QA access.
Every record carries its target. A non-revoked record blocks only a
capability-backed QA handoff for its exact `(project_id, target_application_id)`.
An internal or administrator operator may explicitly drain a revoke only after the
reconciler persisted its terminal `revoke_failed` escalation. That command records
acceptance of unproved remote cleanup and its resolved actor in the durable
work-admission audit.

## Deploy diagnostic redaction

`shared.diagnostics.redact_diagnostic` is the single boundary for runtime and
deploy diagnostics that can leave a process. Callers provide every resolved
secret value, so the boundary removes those values, encoded dotenv payloads
that decode to them, authorization-header values, URL userinfo, and Telegram
Bot API endpoint tokens. The deployer applies it to provider, workflow, HTTP,
dotenv, refusal, cancellation, and unexpected-failure logs and results; smoke
applies it to Bot API failures, SSH failures, and retrieved container logs.
The safe diagnostic retains its failure classification and non-secret context.
