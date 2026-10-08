# Deploy a repaired head after `images_not_published`

The merged-PR poller deploys a story only once the product's own `ci.yml` run for the
built commit on the default branch has published its images. When that run fails, or
the 15-minute bound from the merge runs out, the poller refuses the story: it writes
`quarantine_reason` with `deploy_outcome` `images_not_published`, the PR head
(`head_sha`), the refused commit (`deployed_commit_sha`) and the CI run it read, and
parks the story in `waiting_human_review` with an unreleased engineering stop.

The refused commit's images can never appear, because its CI run is over. If the product's
CI is broken, for example its build job, someone repairs it with a new commit on the
default branch. That commit's push run then publishes images. This runbook puts the story
back on the poller path with an administrator's approval to deploy that repaired head.

## 1. When to use it

Use it only when all of these hold:

- `GET /api/stories/<story_id>` shows `status` `waiting_human_review`,
  `quarantine_reason.deploy_outcome` `images_not_published` and an `engineering_stop`
  with no `released_at`. Note `engineering_stop.id` and `quarantine_reason.deployed_commit_sha`.
- The product's CI has been repaired on the default branch, and the repaired commit's own
  `ci.yml` push run has concluded `success`, so its images are published
  (`sha-<short sha>` for every service).
- The repaired commit is on the default branch and strictly descends from
  `quarantine_reason.deployed_commit_sha`.
- No deploy Run of the story is `queued` or `running`.

Do not use it for a QA park (use `recheck-qa`), or to redeploy an application that has no
story (use `POST /api/applications/<id>/redeploy`).

## 2. Approve the repaired head

Use an authenticated admin Bearer token against the deployed API.

```http
POST /api/stories/<story_id>/deploy-repaired-head
Authorization: Bearer <admin-token>
Content-Type: application/json

{"stop_id": "<engineering_stop.id>", "deployed_commit_sha": "<40-hex repaired commit>"}
```

`deployed_commit_sha` must be the full 40-character lowercase commit. A short SHA or an
image tag is refused with 422 and changes nothing.

On success the API answers 200 with the story. In one transaction it:

- releases the stop, with its `engineering_stop` audit row;
- records the approval in `generated_product_timeline.repaired_head_deploy_approval`:
  the actor, `approved_at`, the PR number, PR head and merge commit, the approved commit
  (`approved_commit_sha`), the commit it supersedes (`superseded_commit_sha`) and the
  quarantine it cleared. Only the API writes this key: a story PATCH that carries it from
  anyone but an internal service is refused with 403;
- clears `quarantine_reason`;
- moves the story `waiting_human_review` → `pr_review`.

The API creates no Run and publishes nothing. The poller does that on its next tick.

Each unmet precondition is a 409 that changes nothing. The body is
`{"code": ..., "detail": ...}`:

| Code | Meaning |
| --- | --- |
| `story_not_waiting_human_review` | The story is not parked for human review (also a repeated call) |
| `not_images_not_published` | The park is not an `images_not_published` refusal |
| `engineering_stop_mismatch` | `stop_id` is not the story's unreleased stop |
| `story_has_no_pull_request` | The story records no `pr_number` |
| `deploy_run_live` | A deploy Run of the story is `queued` or `running` |
| `refused_commit_unknown` | The refusal names no 40-hex `deployed_commit_sha` |
| `repository_missing` | The project has no primary repository |
| `repository_unowned` | The repository is not a GitHub repository URL |
| `pull_request_not_merged` | GitHub does not report the story's PR as merged |
| `pull_request_changed` | The PR head is not the `head_sha` the refusal recorded |
| `commit_not_on_default_branch` | The commit is not on the repository's default branch |
| `commit_not_ahead` | The commit equals, is behind, or diverged from the refused commit |

A repeated call after a success answers 409 `story_not_waiting_human_review`.

## 3. What to watch

- On the next tick the poller logs `poll_merged_repaired_head_approved` with the approved
  and merge commits. It asks for the approved commit's `ci.yml` run on the default branch.
  The 15-minute bound runs from `approved_at`, not from the merge.
- After `poll_merged_images_published` the poller takes its ordinary path. It decides
  CREATE or FEATURE, runs the initial-owner seed lifecycle where it applies, and creates
  the deploy Run `deploy-approved-<digest>`. That Run is distinct from any earlier attempt.
  Its metadata names the PR head (`head_sha`), the deployed commit
  (`deployed_commit_sha` = the approved commit), the `merge_commit_sha` and the approval.
  The story moves to `deploying`.
- The deploy message carries the story id. The deploy handler therefore seeds the product
  settings, issues the platform key and hands the story to QA as for any story deploy.
  QA then completes the story.
- If the approved commit's images do not appear either, the poller refuses the story
  again. The new `quarantine_reason.deployed_commit_sha` is the approved commit. Repair
  CI again, then approve a commit that descends from that one, starting at section 1.
