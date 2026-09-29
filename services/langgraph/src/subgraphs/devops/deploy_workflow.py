"""Bounded recognition of the released executable IPv6 deploy transport.

This recognizes one reviewed workflow shape, not arbitrary shell semantics.
Names, YAML formatting and outer comments may differ; execution must not.
"""

from hashlib import sha256
from ipaddress import IPv6Address, ip_address
import json
import re

import yaml

from shared.clients.github import GitHubAppClient

from .secret_resolver import SecretResolutionError

WORKFLOW_PATH = ".github/workflows/deploy.yml"
_MAX_WORKFLOW_LENGTH = 65_536
# Digest of the released steps, excluding presentation names. Proven against the
# actual production-pin Copier render by test_deploy_workflow_admission.py.
RELEASED_STEPS_DIGEST = "896588bcb8b4a753e08c575440717940c580a5511ae795a6de67a6d7cb13006a"
_REFUSAL = (
    f"{WORKFLOW_PATH}: DEPLOY_HOST IPv6 transport is unverified; "
    "a reviewed kit update and reconciled workflow at the built commit are required"
)


class _UniqueLoader(yaml.BaseLoader):
    """Do not let a repeated YAML key hide which job or step executes."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("ambiguous workflow mapping")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def corrected_ipv6_workflow(source: str | None) -> bool:
    """Recognize selected, unconditional release steps in the sole deploy job."""
    if not isinstance(source, str) or len(source) > _MAX_WORKFLOW_LENGTH:
        return False
    try:
        if any(
            isinstance(token, (yaml.AliasToken, yaml.AnchorToken)) for token in yaml.scan(source)
        ):
            return False
        workflow = yaml.load(source, Loader=_UniqueLoader)  # noqa: S506 (BaseLoader only constructs strings)
        if not isinstance(workflow, dict) or set(workflow) - {"name", "on", "jobs", "permissions"}:
            return False
        if workflow["on"] != {"workflow_dispatch": ""} or set(workflow["jobs"]) != {"deploy"}:
            return False
        job = workflow["jobs"]["deploy"]
        if set(job) - {"name", "runs-on", "steps", "timeout-minutes", "permissions"}:
            return False
        if job["runs-on"] != "ubuntu-24.04":
            return False
        steps = [
            {key: value for key, value in step.items() if key != "name"} for step in job["steps"]
        ]
        digest = sha256(
            json.dumps(steps, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return digest == RELEASED_STEPS_DIGEST
    except (yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError):
        return False


async def require_backend_workflow(
    github: GitHubAppClient, owner: str, repo: str, host: str, built_sha: str
) -> None:
    """Read the built commit before deploy effects; never expose source or read errors."""
    if not isinstance(ip_address(host), IPv6Address):
        return
    if not re.fullmatch(r"[0-9a-f]{40}", built_sha):
        raise SecretResolutionError(_REFUSAL)
    try:
        source = await github.get_file_contents(owner, repo, WORKFLOW_PATH, ref=built_sha)
        if not corrected_ipv6_workflow(source):
            raise SecretResolutionError(_REFUSAL)
        rejected = await github.get_file_contents(
            owner, repo, WORKFLOW_PATH + ".rej", ref=built_sha
        )
    except Exception as error:
        raise SecretResolutionError(_REFUSAL) from error
    if rejected is not None:
        raise SecretResolutionError(_REFUSAL)
