"""Broad validation for the typed production-admin overview card."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    for command, cwd in (
        (["make", "test-unit"], ROOT),
        (["make", "lint"], ROOT),
        (["npm", "test"], ROOT / "services/admin-frontend"),
        (["npm", "run", "lint"], ROOT / "services/admin-frontend"),
        (["npm", "run", "build"], ROOT / "services/admin-frontend"),
    ):
        subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    main()
