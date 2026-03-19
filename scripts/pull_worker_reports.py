#!/usr/bin/env python3
"""Pull worker reports from task events API and save to docs/e2e_results/worker_reports/.

Usage:
    python scripts/pull_worker_reports.py [--since HOURS] [--all]

Options:
    --since HOURS   Pull reports from tasks updated in the last N hours (default: 24)
    --all           Pull all worker reports (ignore time filter)
"""

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
import urllib.request  # noqa: S310 — only connects to localhost API

API_BASE = "http://localhost:8000/api"
REPORTS_DIR = Path("docs/e2e_results/worker_reports")


def api_get(path: str) -> list | dict:
    url = f"{API_BASE}/{path}"
    req = urllib.request.Request(url)  # noqa: S310
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


# Cache to avoid repeated API calls
_project_cache: dict[str, dict] = {}


def get_project(project_id: str) -> dict:
    """Get project details, cached."""
    if project_id not in _project_cache:
        try:
            _project_cache[project_id] = api_get(f"projects/{project_id}")
        except Exception:
            _project_cache[project_id] = {}
    return _project_cache[project_id]


def get_recent_tasks(since_hours: int | None) -> list[dict]:
    """Get tasks, optionally filtered by update time."""
    if since_hours is not None:
        since = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return api_get(f"tasks/?since={since}&sort=-created_at")
    return api_get("tasks/?sort=-created_at")


def get_worker_report_events(task_id: str) -> list[dict]:
    """Get worker_report events for a task."""
    return api_get(f"tasks/{task_id}/events?event_type=worker_report")


def main():
    parser = argparse.ArgumentParser(description="Pull worker reports from API")
    parser.add_argument("--since", type=int, default=24, help="Hours to look back (default: 24)")
    parser.add_argument("--all", action="store_true", help="Pull all reports")
    args = parser.parse_args()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    since_hours = None if args.all else args.since
    tasks = get_recent_tasks(since_hours)

    if not tasks:
        print("No tasks found.")
        return

    saved = 0
    for task in tasks:
        task_id = task["id"]
        events = get_worker_report_events(task_id)

        for event in events:
            report = event.get("details", {}).get("report", "")
            if not report:
                continue

            # Build filename: <project_name>-worker-<date>-<task_id_short>.md
            project = get_project(task.get("project_id", ""))
            project_name = project.get("name", "unknown")
            created = task.get("created_at", "")[:10]  # YYYY-MM-DD
            task_short = task_id.split("-")[-1][:8] if "-" in task_id else task_id[:8]

            filename = f"{project_name}-worker-{created}-{task_short}.md"
            filepath = REPORTS_DIR / filename

            if filepath.exists():
                continue  # Don't overwrite

            task_title = task.get("title", "unknown")
            filepath.write_text(report)
            print(f"  Saved: {filepath} ({task_title})")
            saved += 1

    print(f"\nDone. {saved} new report(s) saved to {REPORTS_DIR}/")
    if saved == 0:
        print("(All reports already existed or no worker_report events found)")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        print(f"Error: Cannot connect to API at {API_BASE} — {e}", file=sys.stderr)
        sys.exit(1)
