"""Unit test configuration.

Sets required env vars before module imports trigger Settings validation.
"""

import os
from pathlib import Path
import sys

# Must set env vars BEFORE any src imports (Settings validates on import)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("API_BASE_URL", "http://localhost:8000")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-for-unit-tests")

# Add /app to sys.path so that 'src' module can be imported.
app_path = Path("/app")
if app_path.exists() and str(app_path) not in sys.path:
    sys.path.insert(0, str(app_path))
