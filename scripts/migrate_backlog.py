#!/usr/bin/env python3
"""Migrate backlog.md Queue section into work_items via API.

Usage:
    python scripts/migrate_backlog.py [--api-url http://localhost:8000] [--dry-run]

Only migrates Queue items (not Done or Ideas).
"""

import argparse
from http import HTTPStatus
import re
import sys

import httpx


def parse_queue(backlog_path: str) -> list[dict]:
    """Parse Queue section of backlog.md into work item dicts."""
    with open(backlog_path) as f:
        content = f.read()

    # Extract Queue section
    queue_match = re.search(
        r"## Queue.*?\n(.*?)(?=\n## (?:Ideas|Done)|\Z)",
        content,
        re.DOTALL,
    )
    if not queue_match:
        print("No Queue section found in backlog.md")
        return []

    queue_text = queue_match.group(1)

    items = []
    current = None

    for line in queue_text.split("\n"):
        # Match task header: ### #55 Title
        header_match = re.match(r"^### #(\d+)\s+(.*)", line)
        if header_match:
            if current:
                items.append(current)
            task_id = int(header_match.group(1))
            title = header_match.group(2).strip()
            current = {
                "backlog_id": task_id,
                "title": title,
                "description": "",
                "priority": len(items),  # Order in queue = priority
                "type": "feature",
            }
            continue

        if current is None:
            continue

        # Match fields
        if line.startswith("- **Priority**:"):
            pass  # We use queue order as priority
        elif line.startswith("- **Brief**:"):
            brief = line.replace("- **Brief**:", "").strip()
            current["description"] = brief
        elif line.startswith("- **Status**:"):
            status = line.replace("- **Status**:", "").strip()
            if status == "in_progress":
                current["status_override"] = "in_dev"

    if current:
        items.append(current)

    return items


def migrate(items: list[dict], api_url: str, dry_run: bool = False) -> None:
    """Create work items via API."""
    print(f"Found {len(items)} items to migrate")
    if dry_run:
        print("DRY RUN — no API calls")

    for item in items:
        payload = {
            "title": f"#{item['backlog_id']} {item['title']}",
            "type": item["type"],
            "description": item["description"],
            "priority": item["priority"],
            "created_by": "migration",
        }

        print(f"  #{item['backlog_id']} {item['title'][:60]} (priority={item['priority']})")

        if dry_run:
            continue

        resp = httpx.post(f"{api_url}/api/work-items/", json=payload, timeout=10)
        if resp.status_code == HTTPStatus.CREATED:
            wi = resp.json()
            print(f"    → created {wi['id']}")

            # If task was in_progress, start it
            if item.get("status_override") == "in_dev":
                start_resp = httpx.post(
                    f"{api_url}/api/work-items/{wi['id']}/start",
                    json={"actor": "migration"},
                    timeout=10,
                )
                if start_resp.status_code == HTTPStatus.OK:
                    print("    → started (in_dev)")
                else:
                    print(f"    → failed to start: {start_resp.text}")
        else:
            print(f"    → FAILED: {resp.status_code} {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="Migrate backlog.md Queue → work_items API")
    parser.add_argument("--api-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no API calls")
    parser.add_argument(
        "--backlog",
        default="docs/backlog.md",
        help="Path to backlog.md",
    )
    args = parser.parse_args()

    items = parse_queue(args.backlog)
    if not items:
        print("No items to migrate")
        sys.exit(0)

    migrate(items, args.api_url, args.dry_run)
    print("Done!")


if __name__ == "__main__":
    main()
