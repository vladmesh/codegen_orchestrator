"""The activated catalog snapshot as the planner reads it, from genuine bytes.

`fixtures/catalog-activated/` holds `packages/catalog.yaml` at the activated commit
(`shared/catalog_activation.yaml`) and the default binding and manifest of the published
`tg-channels` 0.1.2 and `reminders` 0.5.0 tags, unchanged, so the preview and the plan
are exercised on the catalog production plans from, without the network.
"""

from __future__ import annotations

from dataclasses import replace
from functools import cache
import hashlib
from pathlib import Path

from framework.catalog import parse_catalog

from shared.catalog_activation import CATALOG_ACTIVATION
from src.kit_catalog import KitCatalog, installable

DATA = Path(__file__).parent / "fixtures" / "catalog-activated"


@cache
def activated_catalog() -> KitCatalog:
    raw = (DATA / "catalog.yaml").read_text()
    catalog = installable(parse_catalog(raw), CATALOG_ACTIVATION.raw_source)
    return replace(
        catalog,
        raw=raw,
        bindings={
            name: (DATA / f"{name}.default.yaml").read_text()
            for name in ("tg-channels", "reminders")
        },
        manifests={
            name: (DATA / f"{name}.package.yaml").read_text()
            for name in ("tg-channels", "reminders")
        },
        repository=CATALOG_ACTIVATION.repository,
        commit=CATALOG_ACTIVATION.commit,
        catalog_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
