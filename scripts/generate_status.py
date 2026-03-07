#!/usr/bin/env python3
"""Generate docs/STATUS.md from Tasks API.

Usage:
    python scripts/generate_status.py [--api-url URL] [--output PATH]
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_OUTPUT = "docs/STATUS.md"


async def fetch_status_data(api_url: str) -> dict:
    """Fetch current status data from the API."""
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        stats_resp = await client.get("/api/tasks/stats")
        stats_resp.raise_for_status()
        stats = stats_resp.json()

        in_dev_resp = await client.get("/api/tasks/", params={"status": "in_dev", "limit": "1"})
        in_dev_resp.raise_for_status()
        in_dev = in_dev_resp.json()

        recent_done_resp = await client.get(
            "/api/tasks/",
            params={"status": "done", "sort": "-created_at", "limit": "5"},
        )
        recent_done_resp.raise_for_status()
        recent_done = recent_done_resp.json()

        events = []
        if in_dev:
            task_id = in_dev[0]["id"]
            events_resp = await client.get(f"/api/tasks/{task_id}/events")
            events_resp.raise_for_status()
            events = events_resp.json()

    return {
        "stats": stats,
        "in_dev": in_dev[0] if in_dev else None,
        "events": events,
        "recent_done": recent_done,
    }


def format_status(data: dict) -> str:
    """Format status data into STATUS.md markdown."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        "# STATUS",
        "",
        "> [!WARNING]",
        "> Этот файл автогенерируется командой `make sync`."
        " Не редактируйте вручную — изменения будут перезаписаны.",
        "",
        f"> **Updated**: {today}",
        "",
    ]

    # Current task
    lines.append("## Current Task")
    lines.append("")
    task = data.get("in_dev")
    if task:
        lines.append(f"**{task['title']}** (`{task['id']}`)")
        lines.append(f"- **Status**: {task['status']}")
        if task.get("plan"):
            lines.append("- **Plan**: yes")
        lines.append(f"- **Elapsed**: {task.get('elapsed_minutes', 0):.0f} min")
        lines.append("")

        # Recent events
        events = data.get("events", [])
        if events:
            lines.append("### Recent Events")
            lines.append("")
            for ev in events[-10:]:
                ts = ev["created_at"][:16].replace("T", " ")
                if ev["event_type"] == "status_change":
                    lines.append(f"- `{ts}` {ev['from_status']} → {ev['to_status']}")
                else:
                    details = ev.get("details", {})
                    action = details.get("action", ev["event_type"])
                    lines.append(f"- `{ts}` {action}")
            lines.append("")
    else:
        lines.append("_(no task in progress)_")
        lines.append("")

    # Stats
    lines.append("## Stats")
    lines.append("")
    stats = data.get("stats", {})
    parts = []
    for key in ["backlog", "todo", "in_dev", "in_ci", "testing", "done"]:
        if key in stats:
            parts.append(f"{key}: {stats[key]}")
    if parts:
        lines.append("| " + " | ".join(parts) + " |")
    lines.append("")

    # Recent done
    lines.append("## Recently Completed")
    lines.append("")
    recent_done = data.get("recent_done", [])
    if recent_done:
        for item in recent_done:
            updated = item.get("updated_at", "")[:10]
            lines.append(f"- {item['title']} — {updated}")
    else:
        lines.append("_(none)_")
    lines.append("")

    # Quick links
    lines.append("## Quick Links")
    lines.append("")
    lines.append("- [Backlog](backlog.md)")
    lines.append("- [Roadmap](ROADMAP.md)")
    lines.append("- [Changelog](CHANGELOG.md)")
    lines.append("")

    return "\n".join(lines)


async def main(api_url: str, output: str) -> None:
    data = await fetch_status_data(api_url)
    content = format_status(data)

    Path(output).write_text(content)
    print(f"Generated {output}")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Generate STATUS.md from API")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    asyncio.run(main(args.api_url, args.output))
