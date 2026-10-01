"""Prompt loading utilities for langgraph service."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_developer_instructions() -> str:
    """Load the required developer-worker instruction contract.

    A missing or empty instruction file is a packaging error. Starting a worker
    with a one-line fallback silently changes its operating contract, so fail
    before publishing the worker command instead.
    """
    instructions_file = PROMPTS_DIR / "developer_worker" / "INSTRUCTIONS.md"
    try:
        instructions = instructions_file.read_text()
    except FileNotFoundError as error:
        raise RuntimeError(f"developer instructions missing: {instructions_file}") from error
    if not instructions.strip():
        raise RuntimeError(f"developer instructions are empty: {instructions_file}")
    return instructions
