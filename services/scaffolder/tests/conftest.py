"""Pytest configuration for scaffolder tests."""

from pathlib import Path
import sys

# Add scaffolder src to path for imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

# Add project root for shared imports
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))
