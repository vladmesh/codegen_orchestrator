"""The one place the Copier template pin is written down.

`scripts/system_configs.yaml` is what a deployed orchestrator actually reads, so it is
the definition rather than a copy of one: this module parses that seed and hands every
other site the same values — the live suite's scaffold defaults, the stage-5 smoke, the
rendered fixture's directory name and the CI gate's exclusion for it. Nothing else in the
repository spells the source or the ref out again, so moving the pin is one edit here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_CONFIGS_PATH = ROOT / "scripts" / "system_configs.yaml"
SOURCE_KEY = "scheduler.service_template_source"
REF_KEY = "scheduler.service_template_ref"
# Where the rendered template fixture is vendored. The directory name encodes the ref, so
# it is derived here instead of being typed out beside each reader.
FIXTURES_RELPATH = "shared/tests/fixtures"
FIXTURE_PREFIX = "service-template-"


@dataclass(frozen=True)
class TemplatePin:
    """The template revision the orchestrator scaffolds from, and what follows from it."""

    source: str
    ref: str

    @property
    def fixture_dirname(self) -> str:
        return f"{FIXTURE_PREFIX}{self.ref}"

    @property
    def fixture_relpath(self) -> str:
        return f"{FIXTURES_RELPATH}/{self.fixture_dirname}"

    def fixture_path(self, root: Path = ROOT) -> Path:
        return root / FIXTURES_RELPATH / self.fixture_dirname


def _seeded_value(configs: list[dict], key: str, path: Path) -> str:
    values = [entry["value"] for entry in configs if entry["key"] == key]
    if len(values) != 1:
        raise RuntimeError(f"expected exactly one {key} in {path}, found {values}")
    return str(values[0])


def load_template_pin(path: Path = SYSTEM_CONFIGS_PATH) -> TemplatePin:
    """Read the pin from the production seed config, failing loudly on a missing key."""
    configs = yaml.safe_load(path.read_text())
    return TemplatePin(
        source=_seeded_value(configs, SOURCE_KEY, path),
        ref=_seeded_value(configs, REF_KEY, path),
    )


TEMPLATE_PIN = load_template_pin()
