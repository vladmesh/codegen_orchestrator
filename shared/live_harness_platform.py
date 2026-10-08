"""Read-only stand proof for a catalog-selected platform environment declaration."""

import argparse
import asyncio
import hashlib
import json
import os

import httpx
import structlog

from shared.live_harness_mechanical_readback import read_deployment, read_publication
from shared.log_config import setup_logging
from src.clients.api import api_client


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def issuance_proof(project, manifest, contract, admin):
    declared = {
        entry["name"]: entry["source"]
        for entry in manifest["environment"]
        if "source" in entry and entry["source"]["kind"] in {"platform_key", "platform_base_url"}
    }
    if {source["kind"] for source in declared.values()} != {"platform_key", "platform_base_url"}:
        raise ValueError("installed manifest must declare both platform sources")
    for name, source in declared.items():
        entry = contract["entries"][name]
        if entry["source"] != source["kind"] or any(
            entry[key] != value for key, value in source.items() if key != "kind"
        ):
            raise ValueError("deployed environment contract differs from installed declaration")
    if admin["orchestrator_project_id"] != project or admin["disabled"]:
        raise ValueError("fake platform product does not own the deployed project")
    for source in declared.values():
        if source["kind"] == "platform_key":
            grant = admin["grants"][source["service"]]
            if grant != {key: source[key] for key in ("scopes", "quota")}:
                raise ValueError("fake platform grant differs from installed declaration")
    keys = [key["key_id"] for key in admin["keys"] if key["revoked_at"] is None]
    if len(keys) != 1:
        raise ValueError("fake platform must have one non-revoked key")
    return {
        "product_id": "orch-" + hashlib.sha256(project.encode()).hexdigest()[:58],
        "key_ids": keys,
        "grant_sha256": digest(admin["grants"]),
        "platform_entries_sha256": digest({name: contract["entries"][name] for name in declared}),
        "manifest_sha256": digest(manifest),
    }


async def invoke(args):
    if os.environ["LIVE_CONTOUR"] != "stand":
        raise ValueError("platform stand proof requires the stand contour")
    try:
        component = json.loads(args.component)
        deployment = await read_deployment(args.project_name, args.server, component=component)
        backend = deployment["backend"]["component"]
        product_id = "orch-" + hashlib.sha256(args.project.encode()).hexdigest()[:58]
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                os.environ["PLATFORM_AUTH_ADMIN_URL"].rstrip("/")
                + f"/admin/v1/products/{product_id}",
                headers={"Authorization": "Bearer " + os.environ["PLATFORM_AUTH_ADMIN_TOKEN"]},
            )
            if not response.is_success:
                raise ValueError("fake platform internal read refused")
            proof = issuance_proof(
                args.project, backend["manifest"], backend["contract"], response.json()
            )
        publication = await read_publication(
            args.owner, args.repo, args.base, args.head, merge=args.merge, pr=args.pr
        )
        # Environment entries are declarations, never resolved values; retain only proof hashes.
        backend.pop("manifest")
        backend.pop("contract")
        structlog.get_logger().info(
            "platform_readback",
            result={"deployment": deployment, "publication": publication, "issuance": proof},
        )
    finally:
        await api_client.close()


def main():
    setup_logging(service_name="platform_readback", log_format="json")
    parser = argparse.ArgumentParser()
    for name in (
        "project",
        "project-name",
        "server",
        "owner",
        "repo",
        "base",
        "head",
        "story",
        "merge",
        "component",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--pr", type=int, required=True)
    asyncio.run(invoke(parser.parse_args()))


if __name__ == "__main__":
    main()
