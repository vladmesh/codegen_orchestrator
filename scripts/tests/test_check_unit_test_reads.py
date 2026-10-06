"""`make lint` keeps unit tests off documentation text and their own source."""

from __future__ import annotations

import textwrap

import pytest

from scripts import check_unit_test_reads as check

UNIT_TEST = check.ROOT / "services/svc/tests/unit/test_sample.py"


def _findings(source: str, path=UNIT_TEST) -> list[str]:
    return check.violations_in(path, textwrap.dedent(source))


@pytest.mark.parametrize(
    "source",
    [
        'ROOT = 1\ntext = (ROOT / "docs/DEPLOY.md").read_text()\n',
        'text = (REPO_ROOT / "README.md").read_text()\n',
        'from pathlib import Path\ntext = (Path(__file__).parents[2] / "AGENTS.md").read_text()\n',
        'SURFACES = (".env.example", "docs/DEPLOY.md")\n',
        "import inspect\nimport m\nsource = inspect.getsource(m)\n",
        "from inspect import getsource\n",
    ],
)
def test_a_unit_test_reading_docs_markdown_or_its_source_is_a_finding(source):
    assert _findings(source)


@pytest.mark.parametrize(
    "source",
    [
        # Markdown the test writes into its own temporary workspace.
        'def test_x(tmp_path):\n    (tmp_path / "TASK.md").write_text("x")\n',
        # Prompt text that names a document, and an allow-set of paths.
        'def test_x():\n    assert "docs/contracts/qa.md" in PROMPT\n',
        'ALLOWED = {"scripts/x.yaml", "docs/CHANGELOG.md"}\n',
    ],
)
def test_text_and_temporary_files_are_not_findings(source):
    assert _findings(source) == []


def test_the_architecture_guards_file_is_allowed():
    allowed = UNIT_TEST.with_name(check.ALLOWED_NAME)
    assert _findings('text = (ROOT / "docs/DEPLOY.md").read_text()\n', allowed) == []
