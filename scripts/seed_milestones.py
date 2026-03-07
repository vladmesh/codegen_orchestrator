#!/usr/bin/env python3
"""Seed milestones from existing ROADMAP.md phases.

One-time script to migrate the manual ROADMAP into the Milestones API.
Creates milestones for Phase 1-6 and links existing work items by tag.

Usage:
    python scripts/seed_milestones.py [--api-url URL]
"""

import argparse

import httpx
from httpx import codes

DEFAULT_API_URL = "http://localhost:8000"
PROJECT_ID = "codegen-orchestrator"

# Phases from existing ROADMAP.md
PHASES = [
    {
        "title": "Phase 1: Stable Pipeline (scaffold -> code -> CI -> deploy)",
        "description": "Reliable pipeline from description to working deploy.",
        "status": "completed",
        "sort_order": 0,
        "tags": [3, 5, 9, 22, 6, 1],
    },
    {
        "title": "Phase 2A: Pre-MVP (alpha blockers)",
        "description": "Multi-user isolation, infra prod readiness, core product flow.",
        "status": "completed",
        "sort_order": 1,
        "tags": [30, 27, 31, 32, 33, 24, 23, 25, 29, 34],
    },
    {
        "title": "Phase 2B: Post-alpha stability",
        "description": "Post-alpha feedback. Stability, cleanup, optimization.",
        "status": "open",
        "sort_order": 2,
        "tags": [8, 21, 7, 10],
    },
    {
        "title": "Phase 3: Dev Process Automation & Task Store",
        "description": "Dev automation + internal task management (dogfooding).",
        "status": "open",
        "sort_order": 3,
        "tags": [4],
    },
    {
        "title": "Phase 4: Public Beta",
        "description": "More users. Visibility, filtering, quality.",
        "status": "open",
        "sort_order": 4,
        "tags": [2],
    },
    {
        "title": "Phase 5: Capabilities Expansion",
        "description": "Frontend generation, architect node, incremental modules.",
        "status": "open",
        "sort_order": 5,
        "tags": [],
    },
    {
        "title": "Phase 6: Scale",
        "description": "Worker swarm, cost tracking, self-hosted CI.",
        "status": "open",
        "sort_order": 6,
        "tags": [],
    },
]


async def seed(api_url: str) -> None:
    async with httpx.AsyncClient(base_url=api_url, timeout=10) as client:
        # Check if milestones already exist
        existing = await client.get("/api/milestones/", params={"project_id": PROJECT_ID})
        existing.raise_for_status()
        if existing.json():
            print(f"Milestones already exist ({len(existing.json())}). Skipping seed.")
            return

        for phase in PHASES:
            # Create milestone
            ms_resp = await client.post(
                "/api/milestones/",
                json={
                    "project_id": PROJECT_ID,
                    "title": phase["title"],
                    "description": phase["description"],
                    "sort_order": phase["sort_order"],
                    "created_by": "system",
                },
            )
            ms_resp.raise_for_status()
            ms = ms_resp.json()
            ms_id = ms["id"]
            print(f"Created milestone: {ms['title']} ({ms_id})")

            # Complete if status is completed
            if phase["status"] == "completed":
                await client.post(f"/api/milestones/{ms_id}/complete")
                print("  -> marked completed")

            # Link work items by tag
            for tag in phase["tags"]:
                try:
                    wi_resp = await client.get(f"/api/work-items/by-tag/{tag}")
                    if wi_resp.status_code == codes.NOT_FOUND:
                        print(f"  -> #{tag}: not found, skipping")
                        continue
                    wi_resp.raise_for_status()
                    wi = wi_resp.json()

                    patch_resp = await client.patch(
                        f"/api/work-items/{wi['id']}",
                        json={"milestone_id": ms_id},
                    )
                    patch_resp.raise_for_status()
                    print(f"  -> linked #{tag}: {wi['title']}")
                except httpx.HTTPError as e:
                    print(f"  -> #{tag}: error {e}")

    print("\nDone! Run `make roadmap` to generate ROADMAP.md.")


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="Seed milestones from ROADMAP.md")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    args = parser.parse_args()

    asyncio.run(seed(args.api_url))
