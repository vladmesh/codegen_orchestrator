"""Unit test configuration."""

from pathlib import Path
import sys

# Add /app to sys.path so that 'src' module can be imported.
# This is needed because the volume mount for tests doesn't include the src module.
app_path = Path("/app")
if app_path.exists() and str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))
