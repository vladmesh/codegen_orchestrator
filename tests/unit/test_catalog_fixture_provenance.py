"""The vendored render retains every producer byte from its CI artifact."""

import hashlib
import json
from pathlib import Path

from scripts.template_pin import TEMPLATE_PIN

ROOT = Path(__file__).resolve().parents[2]


def test_fixture_matches_the_retained_ci_producer_hashes():
    proof = json.loads((ROOT / "docs/evidence/catalog-install-fixture.json").read_text())
    fixture = ROOT / "shared/tests/fixtures" / TEMPLATE_PIN.fixture_dirname
    assert proof["source"] == TEMPLATE_PIN.source and proof["ref"] == TEMPLATE_PIN.ref
    assert (fixture / ".copier-answers.yml").read_text() == proof["answers"]
    actual = {
        str(path.relative_to(fixture)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in fixture.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    assert actual == proof["tracked_sha256"]
