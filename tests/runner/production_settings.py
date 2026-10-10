"""The confirmed answers of the runner's order, written into its deployed product and read back.

Run by `tests/runner/activated_snapshot_proof.py` after the harness deployed the installed
product, in the orchestrator's environment (`PYTHONPATH=services/langgraph:.`). Nothing
here chooses a confirmed value: the settings are the confirmed brief's and its stored plan's
(`confirmed_product_settings`, the list the deploy seed writes), written through the
production settings client with the deployment's write capability, and read back by QA's
own pre-judgement check (`_settings_readback_blocker`), which writes nothing.

`--mode seed` writes them, reads them back, then replays the same seed and reads back again.
`--mode negatives` runs after the bot was observed: QA's check must refuse a product whose
starting channels were changed through the same typed client, and a confirmed key the
product does not declare; a seed replay then restores the confirmed values, which QA accepts.

The capability arrives in `SETTINGS_WRITE_CAPABILITY` and appears in no output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from shared.contracts.dto.product_brief import InitialSetting, SettingScope
from src.clients.api import api_client
from src.clients.product_settings import GeneratedServiceSettingsClient
from src.confirmed_settings import confirmed_product_settings
from src.consumers.qa import _settings_readback_blocker

CHANNELS_KEY = "tg_channels.starting_channels"
#: A value no answer chose, written only to prove QA notices it.
MISMATCH = ["runner_mismatch"]
#: A key the installed product does not declare: its readback is refused.
UNDECLARED = InitialSetting(
    key="tg_channels.runner_undeclared", scope=SettingScope.PRODUCT, value=["x"]
)


def _proofs(settings, proofs) -> list[dict]:
    return [
        {
            "key": item.key,
            "written": proof.written,
            "failure": proof.failure.value if proof.failure else None,
        }
        for item, proof in zip(settings, proofs, strict=True)
    ]


async def _seed(url: str, settings) -> list[dict]:
    proofs = await GeneratedServiceSettingsClient(url).seed_and_resolve(
        settings, capability=os.environ["SETTINGS_WRITE_CAPABILITY"]
    )
    return _proofs(settings, proofs)


async def _readback(url: str, settings) -> dict | None:
    blocker = await _settings_readback_blocker(url, settings)
    return blocker.model_dump(mode="json") if blocker else None


async def seed(url: str, brief, settings) -> dict:
    return {
        "brief_id": brief.id,
        "brief_revision": brief.revision,
        "settings": [
            {"key": item.key, "scope": item.scope.value, "value": item.value} for item in settings
        ],
        "seed": await _seed(url, settings),
        "readback": await _readback(url, settings),
        # The same confirmed revision seeded again: the same values, still held.
        "replay": await _seed(url, settings),
        "replay_readback": await _readback(url, settings),
    }


async def negatives(url: str, brief, settings) -> dict:
    channels = next(item for item in settings if item.key == CHANNELS_KEY)
    changed = channels.model_copy(update={"value": MISMATCH})
    result = {
        "brief_id": brief.id,
        "mismatch_write": await _seed(url, [changed]),
        "mismatch": await _readback(url, settings),
        "undeclared": await _readback(url, [*settings, UNDECLARED]),
        "restore": await _seed(url, settings),
        "restored": await _readback(url, settings),
    }
    problems = []
    if not all(item["written"] for item in result["mismatch_write"]):
        problems.append("the mismatching value was not written")
    expected = f"{CHANNELS_KEY} (product): readback_mismatch"
    if result["mismatch"] is None or expected not in result["mismatch"]["received"]:
        problems.append("QA accepted a product holding another channel list")
    refused = f"{UNDECLARED.key} (product): readback_rejected"
    if result["undeclared"] is None or refused not in result["undeclared"]["received"]:
        problems.append("QA accepted a confirmed key the product does not hold")
    if not all(item["written"] for item in result["restore"]) or result["restored"] is not None:
        problems.append("the seed replay did not restore the confirmed values")
    result["problems"] = problems
    return result


async def run(mode: str, story_id: str, url: str) -> dict:
    brief = await api_client.get_product_brief_by_story(story_id)
    if brief is None or brief.confirmed_at is None:
        raise RuntimeError(f"story {story_id} has no confirmed brief")
    settings = await confirmed_product_settings(brief, api_client)
    try:
        return await (seed if mode == "seed" else negatives)(url, brief, settings)
    finally:
        await api_client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("seed", "negatives"), required=True)
    parser.add_argument("--story-id", required=True)
    parser.add_argument("--product-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.mode, args.story_id, args.product_url))
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
