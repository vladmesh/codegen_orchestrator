"""Controller evidence captures retain each real redacted save in call order."""

import json

from src.synthetic_buyer.evidence import EVIDENCE_FILE, REPORT_FILE, Phase
from tests.unit.synthetic_buyer.fakes import TOKEN, harness


def test_controller_capture_keeps_paired_immutable_redacted_snapshots(tmp_path):
    h = harness(tmp_path)
    h.store.redaction.add(TOKEN)
    h.store.record["echo"] = TOKEN
    h.store.enter(Phase.ORDER)
    h.store.record["verdict"] = {"status": "passed"}
    h.store.enter(Phase.FREEZE)

    first_json, first_report, final_json, final_report = h.evidence_writes
    assert [path.name for path, _ in h.evidence_writes] == [
        EVIDENCE_FILE,
        REPORT_FILE,
        EVIDENCE_FILE,
        REPORT_FILE,
    ]
    before = json.loads(first_json[1])
    after = json.loads(final_json[1])
    assert before["phase"] == Phase.ORDER.value
    assert before["verdict"]["status"] == "running"
    assert after["phase"] == Phase.FREEZE.value
    assert after["verdict"]["status"] == "passed"
    assert "**running**" in first_report[1]
    assert "**passed**" in final_report[1]
    assert h.retained_evidence() == final_json[1]
    assert h.evidence_text() == final_json[1] + final_report[1]
    assert before["echo"] == after["echo"] != TOKEN
    assert h.store.record["echo"] == TOKEN
