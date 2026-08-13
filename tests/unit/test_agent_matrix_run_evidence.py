"""Every combination of the production matrix retains its own evidence artifact.

The matrix runs four worker/QA combinations inside one SSH step and then throws
the host state away: `cleanup_matrix` removes exited worker containers, and the
live harness deletes their Redis metadata. Run 31688808032 is what that costs —
Codex died twice and the summary could say nothing but `failed`.

The artifact itself is produced and asserted offline in
`tests/live/test_run_evidence.py`. What is asserted here is the ordering the
workflow is responsible for: the artifact is collected per combination, inside
that combination's own window, and a combination that emitted none says so
instead of inheriting the previous one's.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-matrix.yml"


def _script(job: str) -> str:
    workflow = yaml.safe_load(MATRIX_WORKFLOW.read_text())
    steps = workflow["jobs"][job]["steps"]
    assert len(steps) == 1, f"the {job} job is one SSH step"
    return steps[0]["with"]["script"]


def test_report_records_the_evidence_artifact_of_each_combination():
    script = _script("matrix")
    header = "qa_agent\\tworker_agent\\tstatus\\tduration_seconds\\trun_evidence\\n"
    assert f"printf '{header}'" in script
    # Line continuations and indentation are noise here; the row is what matters.
    row = " ".join(script.split())
    assert (
        "printf '%s\\t%s\\t%s\\t%s\\t%s\\n' \\ "
        '"$qa_agent" "$worker_agent" "$status" "$duration" "$evidence"'
    ) in row


def test_evidence_is_collected_within_the_combination_that_produced_it():
    """A combination cannot borrow the artifact of the one before it."""
    script = _script("matrix")
    collection = script.index("-name 'run-evidence-*.json'")
    assert '-newermt "@$started"' in script[collection : collection + 200]
    # The report says "missing" rather than leaving the column empty: an absent
    # artifact is a finding about the combination, not a blank cell.
    assert "evidence=missing" in script


def test_evidence_is_collected_before_the_matrix_cleans_up():
    """Cleanup removes the containers the evidence is read from."""
    script = _script("matrix")
    assert script.index("run-evidence-*.json") < script.rindex("cleanup_matrix")


def test_diagnostics_prints_retained_evidence():
    assert "run-evidence-*.json" in _script("diagnostics")
