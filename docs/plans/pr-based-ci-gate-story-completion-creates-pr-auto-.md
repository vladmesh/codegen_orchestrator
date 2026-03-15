# PR-based CI gate: story completion creates PR, auto-merge on green CI

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

The task replaces the polling-based CI gate (`_ci_gate.py`, 531 lines) with a PR-based flow. Currently, when all story tasks finish, the task dispatcher immediately publishes to `deploy:queue`. The CI gate runs inside the engineering consumer, polling GitHub Actions every 15 seconds for up to 60 minutes.

With feature branches (merged in #1011), code now lives on `story/{story_id}` branches. The natural next step: when a story completes, create a PR from story branch → main. CI runs on the PR. On green CI, auto-merge. On red CI, create a "Fix CI" task in the same story. Deploy triggers via the existing push-to-main webhook.

**Current flow**: all tasks done → deploy:queue → deploy worker
**New flow**: all tasks done → create PR → CI on PR → auto-merge → push-to-main webhook → deploy:queue → deploy worker

Key files:
- `services/scheduler/src/tasks/task_dispatcher.py` — `complete_stories()` (story → deploy)
- `services/langgraph/src/consumers/_ci_gate.py` — polling CI gate (531 lines, to be removed)
- `services/langgraph/src/consumers/engineering.py` — uses `_ci_gate` for standalone tasks
- `services/api/src/routers/webhooks.py` — GitHub webhook handler (main branch only)
- `shared/clients/github.py` — GitHub API client (no PR methods yet)
- `shared/contracts/queues/deploy.py` — DeployMessage
- `shared/contracts/dto/story.py` — StoryStatus enum

## Steps

1. [ ] Add PR methods to GitHub client
   - **Input**: `shared/clients/github.py`
   - **Output**: Three new methods: `create_pull_request(owner, repo, head, base, title, body)`, `enable_auto_merge(owner, repo, pr_number)` (via GraphQL mutation), `merge_pull_request(owner, repo, pr_number, merge_method="merge")`
   - **Test**: Unit tests mocking httpx for each method — success, 422 (PR exists), auth error. Test auto-merge with GraphQL response.

2. [ ] Change `complete_stories()` to create PR instead of triggering deploy
   - **Input**: `services/scheduler/src/tasks/task_dispatcher.py`
   - **Output**: When all tasks done: (a) get primary repository, (b) extract owner/repo from git_url or repo name, (c) call `create_pull_request(owner, repo, head=f"story/{story_id}", base="main", title=story.title)`, (d) call `enable_auto_merge()`, (e) transition story to new status `PR_REVIEW` (or reuse `DEPLOYING` — see step 3). Remove `deploy:queue` publish from this path. Keep worker cleanup and next-story trigger.
   - **Test**: Unit test: mock API client + GitHub client, verify PR created with correct branch, auto-merge enabled, no deploy message published. Test error cases (PR creation fails, repo not found).
   - ⚠️ **needs-approval**: May need new StoryStatus `PR_REVIEW` if we don't reuse `DEPLOYING`

3. [ ] Add `PR_REVIEW` story status (or decide to reuse `DEPLOYING`)
   - **Input**: `shared/contracts/dto/story.py`, Story model if DB migration needed
   - **Output**: Add `PR_REVIEW` to StoryStatus enum, add valid transitions: `IN_PROGRESS → PR_REVIEW`, `PR_REVIEW → DEPLOYING | FAILED | IN_PROGRESS`. This clearly separates "waiting for CI on PR" from "deploying to server".
   - **Test**: Unit test for valid transitions including new status.
   - ⚠️ **needs-approval**: New status in shared contracts + possible DB migration

4. [ ] Extend webhook handler to process PR merge events
   - **Input**: `services/api/src/routers/webhooks.py`
   - **Output**: Handle `pull_request` event with `action=closed` and `merged=true`. When a PR from `story/*` branch is merged to main: (a) extract story_id from branch name, (b) look up story + project, (c) transition story to `DEPLOYING`, (d) publish `DeployMessage` to `deploy:queue`. Keep existing `workflow_run` handler for direct pushes to main (non-story deploys, manual pushes).
   - **Test**: Unit tests: PR merged event → deploy triggered. PR closed without merge → ignored. Non-story branch PR → ignored. PR to non-main branch → ignored.

5. [ ] Handle CI failure on PR: create "Fix CI" task
   - **Input**: `services/api/src/routers/webhooks.py`
   - **Output**: Handle `workflow_run` event where `conclusion=failure` and `head_branch` starts with `story/`. Extract story_id, look up story. Create new task "Fix CI: <workflow failure summary>" in the story, with status `backlog`. Transition story from `PR_REVIEW` back to `IN_PROGRESS` (so task dispatcher picks up the fix task). Close the failed PR (it will be recreated after fix).
   - **Test**: Unit tests: CI failure on story branch → fix task created, story reopened. CI failure on main → ignored (existing behavior). CI failure on non-story branch → ignored.

6. [ ] Remove `_ci_gate.py` and its usage in engineering consumer
   - **Input**: `services/langgraph/src/consumers/_ci_gate.py`, `services/langgraph/src/consumers/engineering.py`
   - **Output**: Delete `_ci_gate.py`. Remove `_should_run_ci_gate`, `_run_ci_gate_and_handle_failure` from engineering.py. Remove the import. For standalone tasks (no `planning_task_id`), the worker just pushes code and the task is marked done — CI runs on the PR or push-to-main webhook handles it. Simplify the post-worker-success flow.
   - **Test**: Unit test: engineering consumer processes task without CI gate. Verify no polling, no worker respawn. Existing tests that mock CI gate should be updated or removed.

7. [ ] Integration tests: full PR-based flow
   - **Input**: All modified files
   - **Output**: Integration test covering: (a) task dispatcher creates PR on story completion, (b) webhook receives PR merge → triggers deploy, (c) webhook receives CI failure on PR → creates fix task. Mock GitHub API responses.
   - **Test**: Integration test with real Redis + DB, mocked GitHub API.

