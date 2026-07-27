"""Digest of the deploy-time environment overrides carried by a deploy.

Two deploys of the same commit are only the same deploy when they set the same
extra environment. The digest gives the redundant-deploy shortcut something to
compare without storing the values themselves.
"""

from __future__ import annotations

import hashlib
import json

EMPTY_OVERRIDES_DIGEST = "none"


def env_overrides_digest(overrides: dict[str, str] | None) -> str:
    """Stable digest of *overrides*, or a marker when there are none.

    Absence and an empty mapping are the same thing on purpose: a deploy that sets
    nothing extra must match a recorded deploy that also set nothing, including the
    records written before this field existed.
    """

    if not overrides:
        return EMPTY_OVERRIDES_DIGEST

    canonical = json.dumps(overrides, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
