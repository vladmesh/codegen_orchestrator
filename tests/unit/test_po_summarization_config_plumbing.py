from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

NUMERIC_ENV_NAMES = (
    "SUMMARIZATION_MAX_TOKENS",
    "SUMMARIZATION_TRIGGER_TOKENS",
    "SUMMARIZATION_MAX_SUMMARY_TOKENS",
)

ENV_WIRING_SURFACES = (
    ".env.example",
    "docker-compose.yml",
    ".github/workflows/deploy.yml",
    ".github/workflows/stand-e2e.yml",
    "docs/DEPLOY.md",
)

SYSTEM_CONFIG_KEYS = (
    "llm.summarization_max_tokens",
    "llm.summarization_trigger_tokens",
    "llm.summarization_max_summary_tokens",
)


def test_numeric_po_summarization_tuning_is_not_environment_wired() -> None:
    for relative_path in ENV_WIRING_SURFACES:
        text = (ROOT / relative_path).read_text()
        for env_name in NUMERIC_ENV_NAMES:
            assert env_name not in text, f"retired {env_name} remains in {relative_path}"


def test_numeric_po_summarization_tuning_remains_system_config() -> None:
    text = (ROOT / "scripts/system_configs.yaml").read_text()
    for key in SYSTEM_CONFIG_KEYS:
        assert f"key: {key}" in text
