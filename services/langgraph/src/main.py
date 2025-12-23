"""LangGraph Orchestrator - Main entry point."""

import asyncio
import os

from .graph import create_graph


async def main() -> None:
    """Run the orchestrator."""
    api_url = os.getenv("API_URL")
    if not api_url:
        raise RuntimeError("API_URL is not set")

    graph = create_graph()

    # TODO: Start server or run in polling mode
    print(f"Orchestrator started, API: {api_url}")

    # Keep running
    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
