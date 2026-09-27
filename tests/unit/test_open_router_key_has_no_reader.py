"""The retired OpenRouter key has no reader, so nothing may start depending on it again.

Its only reader was the RAG embedding client, removed with RAG. The LLM openrouter
channel (`services/langgraph/src/llm/openrouter.py`) authenticates with the
per-agent keys instead. The name is assembled below so this file does not match
its own search.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
NAME = "OPEN_ROUTER" + "_KEY"
ALLOWED = {ROOT / "services" / "langgraph" / "src" / "llm" / "openrouter.py"}
SKIPPED_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
# Where a deploy or a developer would hand the value to a service.
CONFIG_CARRIERS = (
    ".env.example",
    ".github/workflows/deploy.yml",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.stand.yml",
)


def _python_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.py")
        if not SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


def test_the_search_sees_the_tree():
    """A guard on the guard: a wrong root must not make the check pass vacuously."""
    assert len(_python_files()) > 500
    assert ALLOWED <= set(_python_files())


def test_no_python_file_but_the_openrouter_channel_names_the_key():
    readers = sorted(
        str(path.relative_to(ROOT))
        for path in _python_files()
        if path not in ALLOWED and NAME in path.read_text(errors="replace")
    )

    assert readers == [], f"{NAME} is retired; these files reference it: {readers}"


def test_no_deploy_or_compose_file_hands_the_key_to_a_service():
    carriers = [name for name in CONFIG_CARRIERS if NAME in (ROOT / name).read_text()]

    assert carriers == [], f"{NAME} is retired; these files still carry it: {carriers}"
