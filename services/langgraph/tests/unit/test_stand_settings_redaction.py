"""The language-write capability stays inside the QA runtime."""

from src.consumers._qa_redaction import REDACTED, QARunRedaction


def test_stored_settings_capability_is_scrubbed_from_reflected_evidence():
    capability = "settings-write-reflection-canary"
    redaction = QARunRedaction.from_stored({"SETTINGS_WRITE_CAPABILITY": capability})
    evidence = {"reply": f"write refused: {capability}", "transcript": [capability]}
    assert redaction.value(evidence) == {
        "reply": f"write refused: {REDACTED}",
        "transcript": [REDACTED],
    }
    assert redaction.text(f"write refused: {capability[:12]}") == f"write refused: {REDACTED}"
