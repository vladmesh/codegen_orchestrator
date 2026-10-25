"""The one launch of a checked-in service script of this directory against the test API."""

import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).parent


def run_service_script(script: str, **env: str) -> None:
    """Run `script` once in the test interpreter; a nonzero exit fails with its output."""
    ran = subprocess.run(
        [sys.executable, "-P", str(HERE / script)],
        env=os.environ | {"API_BASE_URL": os.environ["TEST_API_BASE_URL"], **env},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
