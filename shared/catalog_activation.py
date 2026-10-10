"""The activated kit catalog snapshot, read from its one definition.

`catalog_activation.yaml` beside this module is the definition; every service reads it
through `CATALOG_ACTIVATION`. The API compares a stored capability preview with it before a
brief is confirmed, and the Architect reads the catalog at its commit and verifies the bytes
against it. There is no default and no fallback: a missing or malformed record fails at import.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from shared.contracts.dto.catalog_install import CatalogActivation

ACTIVATION_PATH = Path(__file__).with_name("catalog_activation.yaml")


def load_catalog_activation(path: Path = ACTIVATION_PATH) -> CatalogActivation:
    return CatalogActivation.model_validate(yaml.safe_load(path.read_text()))


CATALOG_ACTIVATION = load_catalog_activation()
