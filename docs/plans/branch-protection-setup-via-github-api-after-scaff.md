# Branch protection setup via GitHub API after scaffold

> [!WARNING]
> Этот файл автогенерируется командой `make sync`. Не редактируйте вручную — изменения будут перезаписаны.

## Context

After scaffolder creates a GitHub repo and pushes the initial commit to main, there are no branch protection rules. This means anyone (or any agent) can push directly to main, bypassing CI and PRs. The pipeline already creates PRs from story branches and uses auto-merge — but without branch protection, direct pushes are still possible.

The scaffolder flow (`_process_full_mode` in `services/scaffolder/src/consumer.py`) currently: creates repo → sets registry secrets → runs copier + push → updates project status. Branch protection must happen **after** the initial push (GitHub requires at least one commit on the branch).

`GitHubAppClient` in `shared/clients/github.py` has no branch protection methods yet.

## Steps

1. [ ] Add `update_branch_protection` method to `GitHubAppClient`
   - **Input**: `shared/clients/github.py`, `shared/tests/mocks/github.py`
   - **Output**: New async method `update_branch_protection(owner, repo, branch, required_checks, require_pr, enforce_admins)` that calls `PUT /repos/{owner}/{repo}/branches/{branch}/protection`. Matching mock method in `MockGitHubClient`.
   - **Test**: Unit test in `shared/tests/clients/test_github.py` — mock HTTP response, verify correct URL/payload/headers. Test default params (require PR=true, enforce_admins=false, required_checks=["ci"]). Test error handling (404 for non-existent branch).

2. [ ] Call branch protection from scaffolder after successful scaffold
   - **Input**: `services/scaffolder/src/consumer.py`
   - **Output**: In `_process_full_mode`, after `run_scaffold` succeeds and before `update_project_status(ACTIVE)`, call `github.update_branch_protection(org, project_name, "main", required_checks=["ci"], require_pr=True)`. Log success/warning. Non-fatal — if protection fails, scaffold still succeeds (log warning, don't block).
   - **Test**: Unit test in `services/scaffolder/tests/unit/test_consumer.py` — verify `update_branch_protection` is called after successful scaffold with correct args. Test that scaffold succeeds even when branch protection fails (mock raises exception). Test that branch protection is NOT called when scaffold fails.

3. [ ] Verify end-to-end with existing test patterns
   - **Input**: All modified files
   - **Output**: `make test-unit` passes, `make lint` passes
   - **Test**: Run full unit test suite + lint. No regressions.

