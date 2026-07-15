"""GitHub App client — split into domain modules, composed via mixins."""

from ._actions import ActionsMixin
from ._base import (
    GitHubAppClientBase,
    WorkflowCancellationUnprovenError,
    WorkflowCancelledError,
    WorkflowNotFoundError,
)
from ._provisioning import ProvisioningMixin
from ._pull_requests import PullRequestsMixin
from ._repos import ReposMixin
from ._secrets import SecretsMixin


class GitHubAppClient(
    ReposMixin,
    SecretsMixin,
    ActionsMixin,
    PullRequestsMixin,
    ProvisioningMixin,
    GitHubAppClientBase,
):
    """Client for authenticated GitHub App interactions.

    Composed from domain mixins:
    - GitHubAppClientBase: auth, tokens, HTTP request handling
    - ReposMixin: repository CRUD, file operations
    - SecretsMixin: GitHub Actions secrets
    - ActionsMixin: workflows, branch protection, CI
    - PullRequestsMixin: PR create/merge/list/close
    - ProvisioningMixin: high-level repo provisioning
    """


__all__ = [
    "GitHubAppClient",
    "WorkflowCancelledError",
    "WorkflowCancellationUnprovenError",
    "WorkflowNotFoundError",
]
