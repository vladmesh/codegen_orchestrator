#!/usr/bin/env python3
"""Sync recent task plans and brainstorms to docs/ as read-only mirrors.

Keeps only artifacts for the current in_dev task + N most recent done tasks.
Deletes files not in the active window.

Usage:
    python scripts/sync_recent_artifacts.py [--api-url URL] [--keep N]
"""

import argparse
from pathlib import Path
import re

import httpx

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_KEEP = 3
PLANS_DIR = Path("docs/plans")
BRAINSTORMS_DIR = Path("docs/brainstorms")


def _slugify(title: str) -> str:
    tag_match = re.match(r"^#(\d+)\s+(.+)", title)
    if tag_match:
        tag, rest = tag_match.groups()
        slug = re.sub(r"[^a-z0-9]+", "-", rest.lower()).strip("-")[:40]
        return f"{tag}-{slug}"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
    return slug


async def fetch_window_tasks(api_url: str, keep: int) -> list[dict]:
    """Fetch in_dev + backlog-with-plan + last N done tasks."""
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        in_dev_resp = await client.get("/api/tasks/", params={"status": "in_dev"})
        in_dev_resp.raise_for_status()
        in_dev = in_dev_resp.json()

        backlog_resp = await client.get("/api/tasks/", params={"status": "backlog"})
        backlog_resp.raise_for_status()
        backlog_with_plan = [t for t in backlog_resp.json() if t.get("plan")]

        done_resp = await client.get(
            "/api/tasks/",
            params={"status": "done", "sort": "-created_at", "limit": str(keep)},
        )
        done_resp.raise_for_status()
        done = done_resp.json()

    return in_dev + backlog_with_plan + done


async def fetch_brainstorm(api_url: str, brainstorm_id: str) -> dict | None:
    """Fetch a brainstorm by ID."""
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        resp = await client.get(f"/api/brainstorms/{brainstorm_id}")
        if resp.status_code == 404:  # noqa: PLR2004
            return None
        resp.raise_for_status()
        return resp.json()


def sync_artifacts(
    tasks: list[dict],
    brainstorms: dict[str, dict],
    plans_dir: Path,
    brainstorms_dir: Path,
) -> dict[str, list[str]]:
    """Write plan/brainstorm files for window tasks, delete the rest.

    Returns dict with 'written' and 'deleted' file lists.
    """
    plans_dir.mkdir(parents=True, exist_ok=True)
    brainstorms_dir.mkdir(parents=True, exist_ok=True)

    expected_plan_files: set[str] = set()
    expected_bs_files: set[str] = set()
    written: list[str] = []
    deleted: list[str] = []

    for task in tasks:
        slug = _slugify(task["title"])

        # Write plan
        if task.get("plan"):
            fname = f"{slug}.md"
            expected_plan_files.add(fname)
            path = plans_dir / fname
            warning = (
                "> [!WARNING]\n"
                "> Этот файл автогенерируется командой `make sync`."
                " Не редактируйте вручную — изменения будут перезаписаны.\n"
            )
            content = f"# {task['title']}\n\n{warning}\n{task['plan']}\n"
            path.write_text(content)
            written.append(str(path))

        # Write brainstorm
        bs_id = task.get("source_brainstorm_id")
        if bs_id and bs_id in brainstorms:
            bs = brainstorms[bs_id]
            fname = f"{slug}.md"
            expected_bs_files.add(fname)
            path = brainstorms_dir / fname
            warning = (
                "> [!WARNING]\n"
                "> Этот файл автогенерируется командой `make sync`."
                " Не редактируйте вручную — изменения будут перезаписаны.\n"
            )
            content = f"# {bs.get('title', task['title'])}\n\n{warning}\n{bs.get('content', '')}\n"
            path.write_text(content)
            written.append(str(path))

    # Delete files not in the active window
    for f in plans_dir.glob("*.md"):
        if f.name not in expected_plan_files:
            f.unlink()
            deleted.append(str(f))

    for f in brainstorms_dir.glob("*.md"):
        if f.name not in expected_bs_files:
            f.unlink()
            deleted.append(str(f))

    return {"written": written, "deleted": deleted}


async def main(api_url: str, keep: int) -> None:
    tasks = await fetch_window_tasks(api_url, keep)

    # Fetch brainstorms for tasks that reference one
    brainstorms: dict[str, dict] = {}
    for task in tasks:
        bs_id = task.get("source_brainstorm_id")
        if bs_id and bs_id not in brainstorms:
            bs = await fetch_brainstorm(api_url, bs_id)
            if bs:
                brainstorms[bs_id] = bs

    result = sync_artifacts(tasks, brainstorms, PLANS_DIR, BRAINSTORMS_DIR)
    print(f"Synced artifacts: {len(result['written'])} written, {len(result['deleted'])} deleted")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Sync recent task artifacts to docs/")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    args = parser.parse_args()

    asyncio.run(main(args.api_url, args.keep))
