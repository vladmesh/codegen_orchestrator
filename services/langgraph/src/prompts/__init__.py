"""Prompt loading utilities for langgraph service."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent


def load_developer_instructions() -> str:
    """Load developer worker instructions from INSTRUCTIONS.md.

    Returns:
        Markdown content of INSTRUCTIONS.md, or empty string if not found.
    """
    instructions_file = PROMPTS_DIR / "developer_worker" / "INSTRUCTIONS.md"
    if not instructions_file.exists():
        return ""
    return instructions_file.read_text()
