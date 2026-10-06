from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SYSTEM_CONFIG_KEYS = (
    "llm.summarization_max_tokens",
    "llm.summarization_trigger_tokens",
    "llm.summarization_max_summary_tokens",
)


def test_numeric_po_summarization_tuning_remains_system_config() -> None:
    text = (ROOT / "scripts/system_configs.yaml").read_text()
    for key in SYSTEM_CONFIG_KEYS:
        assert f"key: {key}" in text
