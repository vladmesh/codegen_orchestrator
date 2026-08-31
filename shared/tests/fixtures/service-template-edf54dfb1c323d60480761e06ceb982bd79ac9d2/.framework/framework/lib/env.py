"""Environment helpers for script tooling."""

from __future__ import annotations

import os
from pathlib import Path


def get_repo_root() -> Path:
    """Return the repository root, honoring SERVICE_TEMPLATE_ROOT overrides."""

    override = os.environ.get("SERVICE_TEMPLATE_ROOT")
    if override:
        return Path(override).resolve()
    current = Path(__file__).resolve()
    # Try parents[2] (Framework repo structure)
    candidate = current.parents[2]
    if (candidate / "services.yml").exists() or (candidate / "copier.yml").exists():
        return candidate

    # Try parents[3] (Generated project structure: .framework/framework/lib/env.py)
    candidate = current.parents[3]
    if (candidate / "services.yml").exists() or (candidate / ".copier-answers.yml").exists():
        return candidate

    return current.parents[2]
