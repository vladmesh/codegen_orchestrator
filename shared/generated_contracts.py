"""The fixed, non-secret generated contracts readable from a product backend."""

from __future__ import annotations

ACTIVE_PACKAGE_CONTRACT = "codegen_kit/_active_packages.py"
BACKEND_MANIFEST = "services/backend/manifest.yaml"
GENERATED_JOB_REGISTRY = "services/backend/src/generated/jobs_schemas.py"

GENERATED_CONTRACT_PATHS = frozenset(
    {ACTIVE_PACKAGE_CONTRACT, BACKEND_MANIFEST, GENERATED_JOB_REGISTRY}
)
GENERATED_CONTRACT_READ_LIMIT = 262144

# Exit statuses owned by the target wrapper's fixed ``read-contract`` operation.
CONTRACT_PATH_REFUSED = 2
CONTRACT_OUTSIDE_APP = 4
CONTRACT_ABSENT = 5
CONTRACT_TRUNCATED = 6
CONTRACT_UNREADABLE = 7


def validate_generated_contract_path(path: str) -> str:
    """Return one exact contract path, refusing every broader container read."""
    if path not in GENERATED_CONTRACT_PATHS:
        raise ValueError(f"{path!r} is not a readable generated contract")
    return path
