"""Regenerate the committed environment-contract JSON Schema."""

from pathlib import Path

from shared.contracts.env_contract import export_env_contract_json_schema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "shared/contracts/schemas/env-contract.schema.json"


if __name__ == "__main__":
    export_env_contract_json_schema(SCHEMA_PATH)
