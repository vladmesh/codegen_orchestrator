# Managed target readiness

Canonical managed-target reconciliation and readiness contract.

Back to the [contracts index](../CONTRACTS.md).

## Managed target readiness

`shared/qa_target_profile.py` owns the one QA target profile.
`QA_TARGET_PROFILE_VERSION` is derived from every file of the `qa_identity` role
with the wrapper's and the defaults' version lines blanked, and a unit test holds
the constant, both lines and the files together, so any artefact change changes
the version. `qa-docker version` answers `qa-docker profile=<version> verbs=<...>`;
`qa-identity-proof` asks it through the QA account's own sudo rule and fails a
seat that answers anything else, then prints `qa_target_version=<version>`.

The receipt is `servers.qa_target_version` and `servers.qa_target_proved_at`.
Only `POST /api/servers/{handle}/target-readiness` (`TargetReadinessReport` →
`TargetReadinessRead`) and the provisioning finalizer below write it;
`qa_ssh_user`, `provisioning_phase` and PATCH never do.
Every report carries the `TargetIdentity` it was proved over (`ssh_user`,
`host`, `public_ip`, stored-key fingerprint); under the server row lock the
endpoint refuses, with 409 and no change, a verdict whose identity is not the
row's current one. `PATCH /api/servers/{handle}` locks the row too and clears the
receipt in the same transaction whenever that identity changes, server-sync
address updates included.

Readiness owns its own evidence. A not-ready verdict clears the receipt, sets
`target_readiness_failure_phase`, and creates or updates the one active
`target_not_ready` incident (its own unique active index); it moves the row to
`error` only out of an admitting status, recording that status in
`target_readiness_parked_status`, and leaves any other status as it was.
`ssh_key_enc` is never touched. A ready verdict writes the receipt, resolves the
active `target_not_ready` incident and the QA runtime's `step=qa_identity`
refusals, clears the failure phase, and restores the parked status only while
the park still owns the row's `error`: any status write — PATCH, attempt reset,
force rebuild — clears the park's ownership. Other `provisioning_failed`
episodes and statuses are never overwritten or resolved by readiness.

Every provisioning route cuts over credentials before anything is proved. A
bootstrap credential — the BitLaunch creation key, existing host access, a
reinstall's root password — runs only `provision_access.yml`, which installs the
provisioner's public key. `cut_over_to_generated_key` then runs
`target_readiness_login.yml` with the generated private key as the row's
administrative account; the software play runs through that identity, and its
`QATargetProof` carries that account and key fingerprint. A login that fails is a
`credential_cutover` provisioning failure that never reaches the success handler,
and the handler refuses a proof whose fingerprint or account is not the one it
persists and the row administers.

Fresh, existing-access and reinstall success all end at `POST
/api/servers/{handle}/provisioning/finalize` (`ProvisioningFinalization` →
`ProvisioningFinalizationResult`). The command carries the attempt and episode,
the pre-proof row identity, the exact generated-key identity proved by login and
the software play, raw generated key material, the complete-phase labels and the
matching `QATargetReceipt`. The response never carries key material.

The API locks the server row and checks the episode, pre-proof identity, proved
user/host/address/fingerprint, current profile, exact completion labels, parsed
key fingerprint and receipt agreement before its first mutation. It then
encrypts the normalized key, merges the completion labels, records the receipt,
settles the current provisioning episode and only matching readiness evidence,
resets the active episode and writes READY in one transaction. A stale fence or
operator identity edit returns typed `conflict`; malformed or inconsistent
material is `contained`; neither writes anything. The last successful episode
fence remains on the row solely to make an exact redelivery `idempotent`; a
redelivery with different key identity, labels, proof or receipt conflicts.
There is no worker-side key PATCH, completion-label PATCH, read-back or reset.
An unknown HTTP outcome leaves the provisioner stream entry unacknowledged;
before that HTTP call, infra-service stores a delivery-bound copy of the exact
command under a bounded 24-hour TTL, with the whole envelope encrypted by
`SecretsCipher`. PEL reclaim checks this record before constructing a
`ProvisionerNode` and calls only the finalizer with the same attempt, episode,
identity, key, labels and receipt. A typed definitive result clears the record
only after its broker result is published and acknowledged; another unknown
outcome leaves both it and the stream entry pending. Missing,
expired, unavailable, corrupt or delivery-mismatched replay state records a
`provisioning_failed` incident and keeps the target non-admitting instead of
reserving an attempt or rerunning a playbook. Finalizer conflicts after key
cutover also record that incident without reverting the operator's identity
edit. Provisioning-failure settlement requires both the finalized episode and
its pre-proof identity, so success never resolves unrelated or earlier-identity
evidence.

`shared/server_admission.py` refuses a managed row with `target_not_ready` while
a readiness failure phase is recorded, and with `qa_target_receipt_missing` or
`qa_target_receipt_stale`; each is reported as `server_not_provisioned`, never
capacity, and allocation, the scheduler's resource wait and QA read the same
predicate and receipt. Server create and SSH key update accept only an
unencrypted OpenSSH private key with a terminal newline (`shared/ssh_keys.py`),
parse it before commit, keep the encrypted canonical text and
`ssh_key_fingerprint`, and refuse with `ssh_key rejected: <reason>` without
changing the row or echoing key material. A managed row may not be created,
promoted, moved to a complete software phase or have its key cleared into a
keyless state
(`managed_row_requires_admin_key`), except while provisioning owns it and will
mint the key: `pending_setup`, `provisioning`, `force_rebuild` or `reserved`
with no complete software phase — the rows provider discovery and allowlist
adoption create.

`retrofit_qa_identity` reconciles any explicitly managed, phase-complete row
provisioning does not own, without provider authority: stored key parse →
`target_readiness_login.yml` (`admin_login`) → `target_readiness_privilege.yml`
(`privilege_preflight`, non-interactive `become` to uid 0) →
`qa_identity_retrofit.yml` with no `qa_ssh_user` or profile variable → proof
version check → receipt. Each step is its own run with its own timeout and the
first failing run is the phase. `python -m src.provisioner.target_readiness
--revision <sha>` gives every managed row one outcome after a production deploy
and exits zero only when every outcome is a recorded `ready` or `not_ready`;
`in_progress`, `unhandled`, `superseded` and `unrecorded` rows fail it.

A QA harness failure is a typed `QABlocker`, never a product check. A receipt
rejection refuses before any grant; the runner checks the live wrapper right
after the one-shot identity connects; a wrapper refusal is
`qa_target_profile_stale`, and a contract read that ends other than read, absent,
outside `/app` or over the limit is `qa_target_profile_stale` or
`qa_probe_unavailable`. `QA_HARNESS_BLOCKERS` park the story in human review with
an administrator notice naming `recheck-qa` and owner wording that blames no
product; `qa_target_profile_stale` is operator-recheckable.

Every failed check in an executor verdict carries a `cause`, and the runner refuses
one without it or outside `QAFailedCheckCause`: `product`, `qa_capability` (no QA
action for the criterion in the capability catalogue, or one QA never performs, such as
an HTTP write) or `qa_access`
(the product refused the QA identity). A verdict whose top-level `pass` disagrees
with its product and access checks (true with one failed, false with none) is
refused the same way; when its only failures are `qa_capability`, either `pass` is
accepted. A stored `QAFailedCheck` without a cause reads as `product`.

**Unverified checks.** A `qa_capability` check is neither a failure nor a pass. The
runner's `settle_unverified_checks` (`services/langgraph/src/consumers/_qa_runner.py`)
is the one place that decides it: every such check, whatever its origin — `executor`,
an ungrounded `not_applicable` check, a criterion `withheld` before the executor ran
(`agents/qa/acceptance.py`), or a kit `package` row with nothing to exercise it — is
removed from the checks and written to `QARunResult.unverified_checks` as
`{name, reason, origin}` (`shared/contracts/dto/qa_verification.py`). The verdict is
what the remaining checks say: all pass → `passed` (possibly with a non-empty
`unverified_checks`), any product or access failure → `failed`. `QARunResult` also
records `passed_checks`, the names of the checks that ran and passed. The runner
never settles a Run with `qa_capability` in `failed_checks`.

The supervisor puts only `product` checks into a fix task's description and
fingerprint; the task's `qa_failure` evidence carries `unverified_checks` and the
other failed checks as `non_product_failures`. A FAILED run with no `product` check
and a `qa_access` one parks as `qa_checks_unverifiable`, a `QA_HARNESS_BLOCKERS`
member that is operator-recheckable; that blocker names only `qa_access` checks and is
never produced for a capability gap, so the "platform's test environment" wording is
reachable only from a real harness blocker.

**The settling owner event carries the facts.** `OwnerNotification.qa_verification`
and `POSystemEvent.qa_verification` are a `QAVerificationFacts`
(`{qa_run_id, passed_checks, unverified_checks}`), JSON-encoded in its one flat
`po:input` field. The API's completion transaction sets it on `story_completed` from
the passed QA run the completion names; the supervisor sets it on the
`story_quarantined` record of a FAILED or EXHAUSTED verdict. It is `None` on every
other ending. The fix-task route owes no owner event, so it carries none.

**PO tells the user, and keeps the answer.** The PO consumer renders the facts under the
event's text as "What QA checked:" and "What QA could not check:" lines, each check's
name and reason, without the run id, the origin or JSON (`render_qa_verification`,
`services/langgraph/src/consumers/po.py`). When unverified checks are listed, the PO
prompt asks for one message in the user's language that says what was checked, what
could not be and why in plain words, and asks the user to accept it unchecked or change
the requirement. The PO tool `record_unverified_decision(story_id, decision, check_names)`
records the answer with `POST /api/stories/{id}/unverified-decisions`
(`StoryUnverifiedDecisionCreate`: `decision` is `accept_unverified` or
`change_requirement`, the check names, `recorded_by`). The API appends a
`StoryUnverifiedDecision` (`decision`, `check_names`, `qa_run_id`, `decided_at`,
`recorded_by`) to `stories.unverified_decisions` under the story row lock; an earlier
answer is never rewritten. `qa_run_id` is not sent: it is the story's last QA run with
`qa_routed_at`, and a check name that run did not leave unverified is refused (422), as
is a story with no routed run (409). The record changes nothing else — no status, no
reopen, no rerun. `change_requirement` is followed up by PO as a corrected brief
confirmed as its own story. `StoryRead`/`StoryDTO.unverified_decisions` return every
answer, oldest first, to `get_story` and the admin story detail.

**Verification gaps.** After a Run settles as `passed`, `failed` or `exhausted` with
unverified checks, the QA consumer asks `POST /api/projects/{id}/verification-gaps/from-run`
(QA runtime only) to write them in the `verification_gaps` table, read off the settled
Run itself: what (`name`), why (`reason`), `origin`, `story_id`, `run_id` and
`created_at`. It is idempotent per (project, run, check name) and refuses a blocked,
errored or unsettled Run. A write failure is logged and never changes the verdict.
`GET /api/projects/{id}/verification-gaps` (internal or admin) lists them oldest first.

A verdict check may instead be `{"name", "not_applicable": true, "detail"}`, with no
`pass` or `cause`: an input the transport refused, such as an empty Telegram
message. It never counts toward `pass` and is never a failed check, but the runner
keeps it only when paired with a distinct refusal this run's workspace recorded
(`QAWorkspace.transport_refusals`); an unpaired one becomes a `qa_capability` check,
recorded as unverified with origin `not_applicable`. The prompt forbids the form for
an acceptance-criterion check.
