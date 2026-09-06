"""Pytest collection boundaries for shared tests."""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.template_pin import TEMPLATE_PIN  # noqa: E402

# This is a complete generated project fixture. Its own tests execute only in
# the generated project's compatibility smoke, never as orchestrator tests. The
# directory is named after the pinned template, so the glob reads the pin.
collect_ignore_glob = [f"fixtures/{TEMPLATE_PIN.fixture_prefix}*/**"]
