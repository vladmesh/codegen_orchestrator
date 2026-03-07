#!/usr/bin/env python3
"""Generate docs/backlog.md from Tasks API.

Usage:
    python scripts/generate_backlog.py [--api-url URL] [--output PATH]
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

PRIORITY_LABELS = {0: "CRITICAL", 1: "HIGH", 2: "MEDIUM", 3: "LOW"}
BRIEF_MAX_LEN = 300
DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_OUTPUT = "docs/backlog.md"
IDEAS_FILE = "docs/ideas.md"


async def fetch_tasks(api_url: str) -> tuple[list[dict], list[dict]]:
    """Fetch queue and done tasks from the API."""
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        queue_resp = await client.get("/api/tasks/", params={"status": "backlog"})
        queue_resp.raise_for_status()
        queue = queue_resp.json()

        done_resp = await client.get(
            "/api/tasks/",
            params={"status": "done", "sort": "-created_at", "limit": "10"},
        )
        done_resp.raise_for_status()
        done = done_resp.json()

    return queue, done


def format_backlog(queue: list[dict], done: list[dict], ideas_text: str) -> str:
    """Format tasks into backlog markdown."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        "# Backlog",
        "",
        "> [!WARNING]",
        "> **DEPRECATED**: Этот файл более не является источником правды."
        " Задачи мигрировали в базу данных (таблица `tasks`)."
        " Этот файл автогенерируется скриптом исключительно для read-only просмотра.",
        "",
        f"> **Актуально на**: {today} (generated)",
        "",
        "## Queue (ordered by priority, first = next)",
        "",
    ]

    if not queue:
        lines.append("_(empty)_")
        lines.append("")
    else:
        for item in queue:
            tag_title = item["title"]
            lines.append(f"### {tag_title}")
            priority = item.get("priority", 2)
            label = PRIORITY_LABELS.get(priority, "LOW")
            lines.append(f"- **Priority**: {label}")

            plan = item.get("plan")
            if plan:
                lines.append("- **Plan**: yes (in work item)")
            else:
                lines.append("- **Plan**: —")

            lines.append(f"- **Status**: {item['status']}")

            desc = item.get("description")
            if desc:
                brief = desc.replace("\n", " ").strip()
                if len(brief) > BRIEF_MAX_LEN:
                    brief = brief[: BRIEF_MAX_LEN - 3] + "..."
                lines.append(f"- **Brief**: {brief}")

            lines.append("")

    lines.append("")
    lines.append("## Done (last 10)")
    lines.append("")

    if not done:
        lines.append("_(none)_")
        lines.append("")
    else:
        for item in done:
            tag_title = item["title"]
            updated = item.get("updated_at", "")[:10]
            lines.append(f"- {tag_title} — {updated}")
        lines.append("")

    if ideas_text.strip():
        lines.append("## Ideas")
        lines.append("")
        # Strip leading heading + description lines from ideas file
        body = ideas_text.strip()
        for prefix in ("# Ideas", "## Ideas"):
            if body.startswith(prefix):
                body = body[len(prefix) :].strip()
                break
        lines.append(body)
        lines.append("")

    return "\n".join(lines)


async def main(api_url: str, output: str) -> None:
    ideas_text = ""
    ideas_path = Path(IDEAS_FILE)
    if ideas_path.exists():
        ideas_text = ideas_path.read_text()

    queue, done = await fetch_tasks(api_url)
    content = format_backlog(queue, done, ideas_text)

    Path(output).write_text(content)
    print(f"Generated {output} — {len(queue)} queue, {len(done)} done")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Generate backlog.md from API")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    asyncio.run(main(args.api_url, args.output))
