#!/usr/bin/env python3
"""Keep the last deployed worker-image record with a distinct source hash."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

try:
    from scripts.cleanup_worker_images import _load_record, _record_details
except ModuleNotFoundError:  # Script execution adds scripts/, not the repository root, to sys.path.
    from cleanup_worker_images import _load_record, _record_details


def rotate_previous_record(current: Path, previous: Path, next_source_hash: str) -> None:
    """Replace previous only when current is a valid, distinct source generation."""
    if not next_source_hash:
        raise RuntimeError("next source hash is required")
    details = _record_details(_load_record(current))
    if details is None:
        # An unreadable current record must not leave a stale previous record trusted.
        previous.write_text("", encoding="utf-8")
        return
    current_source_hash, _ = details
    if current_source_hash != next_source_hash:
        shutil.copyfile(current, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-record", type=Path, required=True)
    parser.add_argument("--previous-record", type=Path, required=True)
    parser.add_argument("--next-source-hash", required=True)
    args = parser.parse_args()
    rotate_previous_record(args.current_record, args.previous_record, args.next_source_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
