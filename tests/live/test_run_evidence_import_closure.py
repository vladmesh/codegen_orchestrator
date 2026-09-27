"""`run_evidence` imports, and builds a QA cell, where `scripts/` is not importable.

The backend Docker-in-Docker integration runner imports `run_evidence` from
`tests/live` and builds artifacts with it, but its image carries only a few files of
`scripts/`. The location proof reaches `scripts.template_pin` through
`level1_change_set`; imported at module level by `run_evidence` (1399), it turned six
of that suite's tests red on main (CI run 36313353155). The proof is imported only
where a QA Run record is judged, and this guard runs in a fresh interpreter so the
modules other tests already imported cannot hide a regression.
"""

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.needs_no_api_credential

LIVE_TESTS = Path(__file__).resolve().parent
REPO_ROOT = LIVE_TESTS.parents[1]

_WITHOUT_SCRIPTS = textwrap.dedent(
    """
    import importlib.abc
    import json
    import sys

    sys.path[:0] = [{live!r}, {root!r}]


    class RefuseScripts(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "scripts" or name.startswith("scripts."):
                raise ModuleNotFoundError(f"No module named {{name!r}}", name=name)
            return None


    sys.meta_path.insert(0, RefuseScripts())

    import run_evidence

    loaded_on_import = sorted(m for m in sys.modules if m.startswith(("scripts", "level1_")))
    collector = run_evidence.RunEvidenceCollector(run_id="run-1", probe=None)
    cell = run_evidence.qa_cell({{"run_evidence": collector, "qa_requires_executor": False}})
    print(json.dumps({{
        "loaded_on_import": loaded_on_import,
        "location_refusals_accepted": cell["location_refusals_accepted"],
        "loaded_after_qa_cell": sorted(
            m for m in sys.modules if m.startswith(("scripts", "level1_"))
        ),
    }}))
    """
)


def test_run_evidence_imports_and_builds_a_qa_cell_without_scripts(tmp_path):
    code = _WITHOUT_SCRIPTS.format(live=str(LIVE_TESTS), root=str(REPO_ROOT))
    # -I: no PYTHONPATH, no user site, no cwd on the path — only what the code adds.
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    facts = json.loads(result.stdout.strip().splitlines()[-1])
    assert facts == {
        "loaded_on_import": [],
        "location_refusals_accepted": [],
        "loaded_after_qa_cell": [],
    }
