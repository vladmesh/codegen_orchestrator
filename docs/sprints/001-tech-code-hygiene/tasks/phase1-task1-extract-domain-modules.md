# Phase 1 Task 1: Extract domain modules from github.py

## Description
Split `shared/clients/github.py` (1306 LOC) into domain-specific submodules under `shared/clients/github/`. The single-class monolith becomes a package with focused modules.

Target structure:
```
shared/clients/github/
├── __init__.py          # Facade: re-exports GitHubAppClient, WorkflowNotFoundError
├── _base.py             # GitHubAppClient core: __init__, _make_request, _load_private_key, _generate_jwt, auth/token methods (~230 LOC)
├── _repos.py            # Mixin: create_repo, list_org_repos, get_repo, delete_repo, get_file_contents, list_repo_files, create_or_update_file (~180 LOC)
├── _secrets.py           # Mixin: set_repository_secret, set_repository_secrets (~100 LOC)
├── _actions.py          # Mixin: update_branch_protection, enable_repo_auto_merge, trigger_workflow_dispatch, get_latest_workflow_run, wait_for_workflow_completion, get_workflow_failure_logs, rerun_failed_jobs, wait_for_run_completion (~360 LOC)
├── _pull_requests.py    # Mixin: create_pull_request, get_pull_request, enable_auto_merge, merge_pull_request, list_pull_requests, close_pull_request (~230 LOC)
└── _provisioning.py     # Mixin: provision_project_repo (~70 LOC)
```

**Approach**: Use mixin classes. Each `_*.py` defines a mixin (e.g. `ReposMixin`) with methods that call `self._make_request`, `self.get_token`, etc. `_base.py` defines `GitHubAppClientBase` with core auth. `__init__.py` composes them:

```python
class GitHubAppClient(ReposMixin, SecretsMixin, ActionsMixin, PRMixin, ProvisioningMixin, GitHubAppClientBase):
    """GitHub App client — composed from domain mixins."""
```

This preserves the single-class API — every caller still does `GitHubAppClient()` and gets all methods. No import changes needed anywhere.

## Tests First
- Existing tests in `shared/tests/clients/test_github.py` must pass unchanged (they import `GitHubAppClient` and `WorkflowNotFoundError`)
- Verify `from shared.clients.github import GitHubAppClient` still works
- Verify `from shared.clients import GitHubAppClient` still works

## Acceptance Criteria
- [ ] `shared/clients/github/` package exists with `__init__.py`, `_base.py`, `_repos.py`, `_secrets.py`, `_actions.py`, `_pull_requests.py`, `_provisioning.py`
- [ ] Old `shared/clients/github.py` file is removed
- [ ] `GitHubAppClient` class composes all mixins, preserving full API
- [ ] `WorkflowNotFoundError` is importable from `shared.clients.github`
- [ ] All existing tests pass: `make test-unit`
- [ ] No import changes required in any consumer (facade re-exports everything)
- [ ] Each domain module is under 400 LOC
- [ ] `shared/clients/__init__.py` import still works

## Status: pending

## Developer Notes
_To be filled during implementation._
