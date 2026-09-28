"""Run the complete backend DinD Compose target with module import provenance."""

from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[3]
    sys.exit(subprocess.call(["make", "test-integration-backend-dind"], cwd=root))
