"""The confirmed answers of the runner's order, written into its deployed product and read back.

Run by `tests/runner/activated_snapshot_proof.py` after the harness deployed the installed
product, in the orchestrator's environment (`PYTHONPATH=services/langgraph:.`). Nothing
here chooses a value: the settings are the confirmed brief's and its stored plan's
(`confirmed_product_settings`, the list the deploy seed writes), written through the
production settings client with the deployment's write capability, and then read back by
QA's own pre-judgement check (`_settings_readback_blocker`), which writes nothing.

The capability arrives in `SETTINGS_WRITE_CAPABILITY` and appears in no output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from src.clients.api import api_client
from src.clients.product_settings import GeneratedServiceSettingsClient
from src.confirmed_settings import confirmed_product_settings
from src.consumers.qa import _settings_readback_blocker


async def run(story_id: str, product_url: str) -> dict:
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or brief.confirmed_at is None:
        raise RuntimeError(f"story {story_id} has no confirmed brief")
    settings = await confirmed_product_settings(brief, api_client)
    proofs = await GeneratedServiceSettingsClient(product_url).seed_and_resolve(
        settings, capability=os.environ["SETTINGS_WRITE_CAPABILITY"]
    )
    blocker = await _settings_readback_blocker(product_url, settings)
    await api_client.close()
    return {
        "brief_id": brief.id,
        "brief_revision": brief.revision,
        "settings": [
            {"key": item.key, "scope": item.scope.value, "value": item.value} for item in settings
        ],
        "seed": [
            {
                "key": item.key,
                "written": proof.written,
                "failure": proof.failure.value if proof.failure else None,
            }
            for item, proof in zip(settings, proofs, strict=True)
        ],
        "readback": blocker.model_dump(mode="json") if blocker else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--product-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.story_id, args.product_url))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
