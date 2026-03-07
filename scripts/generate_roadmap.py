#!/usr/bin/env python3
"""Generate docs/ROADMAP.md from Milestones API.

Usage:
    python scripts/generate_roadmap.py [--api-url URL] [--output PATH]
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_OUTPUT = "docs/ROADMAP.md"
DONE_STATUSES = {"done", "completed"}


async def fetch_roadmap_data(
    api_url: str, project_id: str
) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    """Fetch milestones and their work items from the API."""
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        ms_resp = await client.get("/api/milestones/", params={"project_id": project_id})
        ms_resp.raise_for_status()
        milestones = ms_resp.json()

        work_items_by_milestone: dict[str, list[dict]] = {}
        for ms in milestones:
            wi_resp = await client.get(f"/api/milestones/{ms['id']}/work-items")
            wi_resp.raise_for_status()
            work_items_by_milestone[ms["id"]] = wi_resp.json()

        # Unsorted: work items without a milestone
        unsorted_resp = await client.get(
            "/api/work-items/",
            params={"project_id": project_id, "status": "backlog"},
        )
        unsorted_resp.raise_for_status()
        all_backlog = unsorted_resp.json()
        unsorted = [wi for wi in all_backlog if not wi.get("milestone_id")]

    return milestones, work_items_by_milestone, unsorted


def format_roadmap(
    milestones: list[dict],
    work_items_by_milestone: dict[str, list[dict]],
    unsorted_items: list[dict],
) -> str:
    """Format milestones and work items into ROADMAP markdown."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        "# Roadmap",
        "",
        f"> **Updated**: {today} (generated)",
        "",
    ]

    for ms in milestones:
        title = ms["title"]
        status = ms["status"]
        description = ms.get("description")

        if status == "completed":
            lines.append(f"## {title} — COMPLETE")
            lines.append("")
            if description:
                lines.append(f"_{description}_")
                lines.append("")
        else:
            lines.append(f"## {title}")
            lines.append("")
            if description:
                lines.append(description)
                lines.append("")

            items = work_items_by_milestone.get(ms["id"], [])
            for wi in items:
                checkbox = "x" if wi["status"] in DONE_STATUSES else " "
                lines.append(f"- [{checkbox}] {wi['title']}")

            if items:
                lines.append("")

    if unsorted_items:
        lines.append("## Backlog")
        lines.append("")
        for wi in unsorted_items:
            checkbox = "x" if wi["status"] in DONE_STATUSES else " "
            lines.append(f"- [{checkbox}] {wi['title']}")
        lines.append("")

    return "\n".join(lines)


async def main(api_url: str, output: str, project_id: str) -> None:
    milestones, work_items_by_milestone, unsorted = await fetch_roadmap_data(api_url, project_id)
    content = format_roadmap(milestones, work_items_by_milestone, unsorted)

    Path(output).write_text(content)
    print(f"Generated {output} — {len(milestones)} milestones")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Generate ROADMAP.md from API")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--project-id", default="codegen-orchestrator")
    args = parser.parse_args()

    asyncio.run(main(args.api_url, args.output, args.project_id))
