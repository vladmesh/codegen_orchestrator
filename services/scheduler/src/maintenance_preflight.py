"""Fail-closed provisioning inventory preflight used by the PO maintenance runbook."""

from __future__ import annotations

import asyncio
import json

from shared.contracts.dto.server import ServerStatus
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    managed_provider_ids,
    normalize_provider_id,
    provider_operation_is_authorized,
)
from shared.server_admission import IN_PROGRESS_TARGET_STATUSES, target_readiness_reconcilable

from .clients.api import api_client
from .tasks.server_sync import get_time4vps_client


def po_maintenance_counts(servers, provider_servers, managed_ids):
    """Return every maintenance blocker that must be zero before dispatch."""

    def completed(row):
        return target_readiness_reconcilable(row) and bool(row.public_ip or row.host)

    def settled_absent(row):
        return (
            row.status == ServerStatus.UNREACHABLE
            and not row.is_managed
            and row.provider_id is not None
            and row.provider_id not in managed_ids
            and normalize_provider_id(row.provider, row.provider_id) == row.provider_id
        )

    rows = [server for server in servers if server.provider == TIME4VPS_PROVIDER]
    by_id = {}
    for row in rows:
        by_id.setdefault(row.provider_id, []).append(row)
    provider_ids = [str(provider.id) for provider in provider_servers]
    drift = len(provider_ids) - len(set(provider_ids))
    drift += sum(len(matches) != 1 for matches in by_id.values())
    drift += sum(row.provider_id not in provider_ids and not settled_absent(row) for row in rows)
    for item in provider_servers:
        matches = by_id.get(str(item.id), [])
        if len(matches) != 1:
            drift += 1
            continue
        row = matches[0]
        drift += int(
            not item.ip
            or row.public_ip != item.ip
            or row.host != (item.domain or item.ip)
            or row.is_managed != (str(item.id) in managed_ids)
        )
    return {
        "scheduled_servers": sum(server.status in IN_PROGRESS_TARGET_STATUSES for server in servers),
        "authorized_pending_setup": sum(
            server.status == ServerStatus.PENDING_SETUP
            and provider_operation_is_authorized(
                provider=server.provider,
                provider_id=server.provider_id,
                is_managed=server.is_managed,
            )
            for server in servers
        ),
        "unreconcilable_managed": sum(
            server.is_managed and not completed(server) for server in servers
        ),
        "allowlist_without_reconciled_row": sum(
            len(by_id.get(provider_id, [])) != 1 or not completed(by_id[provider_id][0])
            for provider_id in managed_ids
        ),
        "provider_inventory_drift": drift,
    }


async def preflight() -> int:
    """Print one bounded JSON verdict and return zero only for a settled inventory."""
    try:
        servers = await api_client.get_servers()
        provider = await get_time4vps_client()
        if provider is None:
            raise RuntimeError("Provider read unavailable")
        async with provider:
            inventory = await provider.get_servers()
        counts = po_maintenance_counts(
            servers,
            inventory,
            managed_provider_ids(TIME4VPS_PROVIDER),
        )
        print(json.dumps(counts, sort_keys=True))
        return int(any(counts.values()))
    except Exception:
        print(json.dumps({"preflight_failed": 1}))
        return 1
    finally:
        await api_client.close()


def main() -> int:
    return asyncio.run(preflight())


if __name__ == "__main__":
    raise SystemExit(main())
