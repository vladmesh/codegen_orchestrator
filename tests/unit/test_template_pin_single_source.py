"""The Copier template pin is written down once, and everything else derives from it.

`scripts/system_configs.yaml` is the production seed a deployed orchestrator reads, so it
is the definition. The sites that used to repeat it — the live suite's scaffold defaults,
the stage-5 smoke, the vendored fixture's directory name and the CI gate's exclusion for
that fixture — now read `scripts.template_pin`, and these tests hold that shape: one
literal in the tree, and a changed definition arriving at every derived site.
"""

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

from scripts import template_pin

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DIR = REPO_ROOT / "tests" / "live"
PIPELINE_HELPERS = LIVE_DIR / "pipeline_helpers.py"
STAGE5_SMOKE = REPO_ROOT / "tests" / "integration" / "template" / "stage5_mock_smoke.py"
CI_GATE = REPO_ROOT / "scripts" / "check-ci-gate.py"
# The seed itself holds the pin, and the CHANGELOG records the day it last moved; both are
# records rather than copies read by code. The fixture tree is a vendored render whose own
# files mention the revision they came from.
LITERAL_ALLOWED = {
    "scripts/system_configs.yaml",
    "docs/CHANGELOG.md",
}
FIXTURE_TREE = "shared/tests/fixtures/"
# Dependency manifests are written by a package resolver and hold third-party versions.
# A release tag collides with one of those versions by coincidence, and no line in them
# can be a copy of the template pin, so they are not searched for it. Everything a human
# writes still is.
DEPENDENCY_MANIFEST_SUFFIXES = (".lock", "lock.json", "requirements.txt")

# A revision the pin does not hold, so moving it in the seed alone is observable at
# every derived site. Never the live pin: that literal belongs to the definition.
CANDIDATE_SOURCE = "gh:vladmesh/some-other-kit"
CANDIDATE_REF = "1" * 40


def _is_dependency_manifest(name: str) -> bool:
    return name.endswith(DEPENDENCY_MANIFEST_SUFFIXES)


def _tracked_files() -> list[str]:
    listing = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return listing.stdout.splitlines()


def _load(name: str, path: Path) -> ModuleType:
    """Import a module from its file, so it reads the pin as it stands right now."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    # Under its probe name, so a dataclass defined in it can resolve its own module
    # while the real module keeps the name the rest of the suite imports.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_pinned_ref_is_a_literal_in_exactly_one_file() -> None:
    ref = template_pin.TEMPLATE_PIN.ref
    carriers = sorted(
        name
        for name in _tracked_files()
        if not name.startswith(FIXTURE_TREE)
        and not _is_dependency_manifest(name)
        and name not in LITERAL_ALLOWED
        and ref in (REPO_ROOT / name).read_text(errors="ignore")
    )

    assert carriers == [], f"the template ref is repeated outside its definition: {carriers}"


@pytest.fixture
def candidate_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> template_pin.TemplatePin:
    """Move the pin in the definition alone, the way the next card will move it."""
    configs = yaml.safe_load(template_pin.SYSTEM_CONFIGS_PATH.read_text())
    for entry in configs:
        if entry["key"] == template_pin.SOURCE_KEY:
            entry["value"] = CANDIDATE_SOURCE
        if entry["key"] == template_pin.REF_KEY:
            entry["value"] = CANDIDATE_REF
    seed = tmp_path / "system_configs.yaml"
    seed.write_text(yaml.safe_dump(configs))

    moved = template_pin.load_template_pin(seed)
    monkeypatch.setattr(template_pin, "SYSTEM_CONFIGS_PATH", seed)
    monkeypatch.setattr(template_pin, "TEMPLATE_PIN", moved)
    monkeypatch.syspath_prepend(str(LIVE_DIR))
    return moved


def test_a_moved_pin_reaches_the_live_suite_defaults(
    candidate_pin: template_pin.TemplatePin,
) -> None:
    helpers = _load("pipeline_helpers_probe", PIPELINE_HELPERS)

    assert helpers.DEFAULT_TEMPLATE_REPO == CANDIDATE_SOURCE
    assert helpers.DEFAULT_TEMPLATE_REF == CANDIDATE_REF
    assert helpers.resolve_template() == (CANDIDATE_SOURCE, CANDIDATE_REF)


def test_a_moved_pin_reaches_the_stage5_smoke(candidate_pin: template_pin.TemplatePin) -> None:
    smoke = _load("stage5_mock_smoke_probe", STAGE5_SMOKE)
    template = smoke.load_production_template()

    assert (template.source, template.ref) == (CANDIDATE_SOURCE, CANDIDATE_REF)


def test_a_moved_pin_reaches_the_fixture_path_and_the_ci_gate(
    candidate_pin: template_pin.TemplatePin,
) -> None:
    gate = _load("check_ci_gate_probe", CI_GATE)

    assert candidate_pin.fixture_path().name == f"some-other-kit-{CANDIDATE_REF}"
    assert candidate_pin.fixture_relpath in gate.UNPINNED_IMAGE_DIRS


def test_the_live_override_still_wins_over_the_moved_pin(
    candidate_pin: template_pin.TemplatePin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LIVE_TEMPLATE_REPO`/`LIVE_TEMPLATE_REF` stay the stand's candidate mechanism."""
    stand_source = "gh:vladmesh/service-template"
    stand_ref = "a" * 40
    helpers = _load("pipeline_helpers_probe", PIPELINE_HELPERS)
    monkeypatch.setenv(helpers.TEMPLATE_REPO_ENV, stand_source)
    monkeypatch.setenv(helpers.TEMPLATE_REF_ENV, stand_ref)

    assert helpers.resolve_template() == (stand_source, stand_ref)
