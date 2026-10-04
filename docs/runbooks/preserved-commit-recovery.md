# Recover a preserved engineering commit

Use the admin/internal API after the candidate passes dispatcher CI and review.
This runbook describes an agent-free operator action. It authorizes no production
action as part of card 1505. DoD10 remains a separate PO/operator step.

1. Read the Story, original failed engineering Run, its Task if present, and
   owned Repository. Record the current `engineering_stop.id`, initiating Run,
   worker ID, `pre_attempt_head_sha`, Task iteration and Story cycle. Confirm no
   newer engineering attempt reused the Project checkout. Read the Run's typed
   publication evidence and worker report; keep the original ledger readback.
2. Stop the Story through `human-review` if needed. Wait for owned teardown;
   `reconcile-engineering-stop` is retryable. A missing removal proof or a live
   workspace lease is a refusal, not permission to start recovery beside a worker.
3. Preserve a separate bundle before any operator workspace repair. For released
   story `story-e7e6a09f`, retained evidence names original Run `eng-310c3744772a`,
   SHA `bb67d29a641b0f2460b3194798ae0cc52033a322`, checkout
   `/data/workspaces/repo-b447fc9b` on h01o and PO bundle
   `/home/vlad/ops-1495/story-e7e6a09f.bundle`. These are evidence to verify in
   DoD10, not caller-selectable paths or production fixtures. An operator restores
   a missing checkout separately. Do not reset, clone over, or delete its only copy.
4. Send authenticated `POST /api/stories/<story>/recover-commit` with
   `attempt_id`, the full exact `commit_sha`, and the exact current `stop_id`.
   Set `adopt_preserved_commit: true` deliberately for the released legacy attempt.
   The server derives repository, target branch and workspace; those cannot be
   supplied in the request. Missing baseline/ownership requires reconciliation
   of trusted attempt evidence, never a guessed value or paid coding rerun.
5. Inspect `GET /api/commit-recoveries/<attempt>`. Success requires
   `receipt.published=true`, exact matching local/remote SHA and `handed_off_at`.
   A refusal remains parked with its typed failure and bounded diagnostic. Fix
   the named credential/ref/workspace condition outside this action, then repeat
   the identical request. Never force-push. If a response is lost, read/replay
   this claim; no new Run or coding turn is necessary.
6. Verify the original Run and ledger are byte-for-byte unchanged, Task completion
   and recovery audit exist once, the matching stop was explicitly released, and
   Story is discoverable in `in_progress`. Observe the normal scheduler's exact
   current-cycle PR, CI, merge, deploy and QA progression. Recovery's publication
   receipt alone proves none of those downstream results.

A newer attempt, cycle/iteration change, different SHA or different/newer stop
refuses continuation. Replaying an already handed-off claim only reads its old
result and cannot clear a later stop. Ordinary paid resume remains a separate
audited budget action and refuses preserved unpublished work.
