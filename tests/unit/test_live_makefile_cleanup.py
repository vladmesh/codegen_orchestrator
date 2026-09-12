"""Regression guards for retired live-test Makefile compatibility targets."""

from pathlib import Path
import re

ROOT = Path(__file__).parents[2]
MAKEFILE = ROOT / "Makefile"
DOCS = (
    ROOT / "docs" / "TESTING.md",
    ROOT / "docs" / "live-deploy-operations.md",
    ROOT / "tests" / "live" / "README.md",
)


def test_retired_live_targets_are_not_defined() -> None:
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()

    assert not any(line.startswith("test-live-mega:") for line in lines)
    assert not any(line.startswith("test-live-pipeline:") for line in lines)
    assert any(line.startswith("test-live-mega-noop:") for line in lines)
    assert any(line.startswith("test-live-mega-brief-package:") for line in lines)


def test_docs_name_canonical_live_targets_only() -> None:
    retired_alias = re.compile(r"\bmake test-live-mega(?=\s|$)")

    for path in DOCS:
        text = path.read_text(encoding="utf-8")
        assert not retired_alias.search(text), path
        assert "make test-live-pipeline" not in text, path
