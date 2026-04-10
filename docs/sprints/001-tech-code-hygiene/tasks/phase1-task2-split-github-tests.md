# Phase 1 Task 2: Split github test file to match domain modules

## Description
Split `shared/tests/clients/test_github.py` (614 LOC) into domain-specific test files matching the new module structure. This keeps test files focused and easier to maintain.

Target structure:
```
shared/tests/clients/
├── test_github_base.py         # Auth, token caching, rate limiting, JWT tests
├── test_github_repos.py        # create_repo, get_repo, delete_repo, file ops tests
├── test_github_secrets.py      # set_repository_secret, set_repository_secrets tests
├── test_github_actions.py      # workflow dispatch, wait, branch protection tests
├── test_github_pull_requests.py # PR create, merge, list, close tests
├── test_github_provisioning.py # provision_project_repo tests
```

All tests still import `from shared.clients.github import GitHubAppClient` — only the test file organization changes.

## Tests First
- All split test files must pass: `pytest shared/tests/clients/test_github_*.py`
- Total test count must match original (no tests lost or duplicated)

## Acceptance Criteria
- [ ] Old `test_github.py` is removed
- [ ] Each domain has its own test file
- [ ] All tests pass: `make test-unit`
- [ ] No test lost — same total count

## Status: pending

## Developer Notes
_To be filled during implementation._
