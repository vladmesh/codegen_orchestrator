#!/usr/bin/env python3
"""Create and clean up the two run-owned machines used by a stand e2e run.

The lifecycle has no production-host fallback. A run owns exactly one
orchestrator and one deploy target, both identified by its immutable run tag.
Names are the portable provider tag: BitLaunch exposes them in list responses,
so cleanup can fail closed even where arbitrary provider labels are unavailable.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import time
from typing import Any, Literal, Protocol, runtime_checkable

from scripts.bitlaunch_stand import (
    CREATE_POLL_SECONDS,
    CREATE_TIMEOUT_SECONDS,
    HOST_ID,
    IMAGE_ID,
    REGION_ID,
    SIZE_ID,
    _api_key,
    _request,
)

Role = Literal["orchestrator", "target"]
ROLES: tuple[Role, Role] = ("orchestrator", "target")
RESOURCE_CEILING = len(ROLES)
NAME_PREFIX = "codegen-stand"
DEFAULT_SSH_KEY_NAME = "stands_ed25519"
DEFAULT_MINIMUM_BALANCE_MILLIUSD = 200


class LifecycleRefusal(RuntimeError):
    """A pre-create or destructive-policy condition refused an operation."""


@dataclass(frozen=True)
class Machine:
    """Public observations of one ephemeral machine. Never carries credentials."""

    id: str
    role: Role
    ip: str | None
    observed_at: str
    run_tag: str | None
    created_at: str | None = None
    hourly_cost_cents: int | None = None


@dataclass(frozen=True)
class RunManifest:
    """Redacted artifact/report input for an ephemeral run."""

    run_tag: str
    observed_at: str
    machines: tuple[Machine, ...]
    resource_ceiling: int
    lifetime_seconds: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_tag": self.run_tag,
            "observed_at": self.observed_at,
            "resource_ceiling": self.resource_ceiling,
            "lifetime_seconds": self.lifetime_seconds,
            "machines": [asdict(machine) for machine in self.machines],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


@runtime_checkable
class DestructionPolicy(Protocol):
    """Provider-neutral authorization for a destructive lifecycle operation."""

    def allows(self, machine: Machine) -> bool: ...


@runtime_checkable
class EphemeralLifecycle(Protocol):
    """Operations every ephemeral provider offers to the stand workflow."""

    def preflight(self, *, run_tag: str) -> None: ...

    def create_run(self, *, run_tag: str) -> RunManifest: ...

    def cleanup_run(self, *, run_tag: str) -> list[str]: ...

    def sweep_expired(self, *, now: datetime, ttl: timedelta) -> list[str]: ...


@dataclass(frozen=True)
class RunTagDestructionPolicy:
    """Allow deletion only for a machine bearing this exact lifecycle run tag."""

    run_tag: str

    def allows(self, machine: Machine) -> bool:
        return machine.run_tag == self.run_tag and machine.role in ROLES


@dataclass(frozen=True)
class Time4VPSDestructionPolicy:
    """Adapt the existing provider-id authorization to the shared policy seam.

    Existing Time4VPS callers supply their already-authorized IDs. The policy
    intentionally does not read a provider-named process-wide allowlist, and
    therefore has the same explicit ownership boundary as a run tag.
    """

    allowed_ids: frozenset[str]

    def allows(self, machine: Machine) -> bool:
        return machine.id in self.allowed_ids


Request = Callable[[str, str, dict[str, Any] | None], object]


def cents_to_usd(raw_milliusd: int) -> str:
    """Render BitLaunch's raw thousandth-dollar balance without float drift."""
    if raw_milliusd < 0:
        raise ValueError("balance_milliusd must not be negative")
    return f"{raw_milliusd // 1000}.{raw_milliusd % 1000 // 10:02d}"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _machine_name(run_tag: str, role: Role) -> str:
    return f"{NAME_PREFIX}-{run_tag}-{role}"


def _machine_from_server(server: object) -> Machine | None:
    if not isinstance(server, dict):
        return None
    name = server.get("name")
    if not isinstance(name, str) or not name.startswith(f"{NAME_PREFIX}-"):
        return None
    tail = name.removeprefix(f"{NAME_PREFIX}-")
    run_tag, separator, role = tail.rpartition("-")
    if not separator or role not in ROLES or not run_tag:
        return None
    server_id = server.get("id")
    if server_id is None:
        return None
    observed_at = server.get("updated") or server.get("created") or _now()
    return Machine(
        id=str(server_id),
        role=role,
        ip=server.get("ipv4") if isinstance(server.get("ipv4"), str) else None,
        observed_at=str(observed_at),
        run_tag=run_tag,
        created_at=str(server["created"]) if server.get("created") is not None else None,
        hourly_cost_cents=server.get("costPerHr")
        if isinstance(server.get("costPerHr"), int)
        else None,
    )


def _parse_observed_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def select_expired_run_machines(
    machines: list[Machine], *, now: datetime, ttl: timedelta
) -> list[Machine]:
    """Select only tagged lifecycle resources old enough for the TTL sweep."""
    selected: list[Machine] = []
    for machine in machines:
        if machine.run_tag is None:
            continue
        observed_at = _parse_observed_at(machine.observed_at)
        if observed_at is not None and now - observed_at >= ttl:
            selected.append(machine)
    return selected


class BitLaunchLifecycle:
    """BitLaunch implementation of the provider-neutral run lifecycle."""

    def __init__(
        self,
        request: Request,
        *,
        ssh_key_name: str = DEFAULT_SSH_KEY_NAME,
        minimum_balance_cents: int = DEFAULT_MINIMUM_BALANCE_MILLIUSD,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._request = request
        self._ssh_key_name = ssh_key_name
        self._minimum_balance_cents = minimum_balance_cents
        self._sleep = sleeper

    def _account_and_key(self) -> tuple[dict[str, Any], str]:
        user = self._request("GET", "user", None)
        keys = self._request("GET", "ssh-keys", None)
        if not isinstance(user, dict):
            raise LifecycleRefusal("account_unusable: BitLaunch user response is malformed")
        if not user.get("emailConfirmed"):
            raise LifecycleRefusal("account_unusable: BitLaunch email is not confirmed")
        balance = user.get("balance")
        if not isinstance(balance, int) or balance < self._minimum_balance_cents:
            shown = (
                cents_to_usd(balance) if isinstance(balance, int) and balance >= 0 else "unknown"
            )
            raise LifecycleRefusal(
                "insufficient_balance: need at least "
                f"{cents_to_usd(self._minimum_balance_cents)} USD; "
                f"available {shown} USD"
            )
        used, limit = user.get("used"), user.get("limit")
        if (
            not isinstance(used, int)
            or not isinstance(limit, int)
            or limit - used < RESOURCE_CEILING
        ):
            raise LifecycleRefusal(
                f"quota_exhausted: need {RESOURCE_CEILING} free server slots for one run"
            )
        if not isinstance(keys, dict):
            raise LifecycleRefusal("ssh_material_missing: SSH key inventory is malformed")
        matching = [
            key.get("id")
            for key in keys.get("keys", [])
            if isinstance(key, dict) and key.get("name") == self._ssh_key_name and key.get("id")
        ]
        if not matching:
            raise LifecycleRefusal(
                f"ssh_material_missing: no usable SSH key named {self._ssh_key_name!r}"
            )
        return user, str(matching[0])

    def preflight(self, *, run_tag: str) -> None:
        if not run_tag or any(character.isspace() for character in run_tag):
            raise LifecycleRefusal(
                "run_tag_invalid: run tag must be non-empty and contain no whitespace"
            )
        self._account_and_key()

    def _existing_for_run(self, run_tag: str) -> list[Machine]:
        servers = self._request("GET", "servers", None)
        if not isinstance(servers, list):
            raise LifecycleRefusal("inventory_unusable: BitLaunch server response is malformed")
        return [
            machine
            for server in servers
            if (machine := _machine_from_server(server)) is not None and machine.run_tag == run_tag
        ]

    def _wait_for_machine(self, machine_id: str, role: Role, run_tag: str) -> Machine:
        deadline = time.monotonic() + CREATE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = self._request("GET", f"servers/{machine_id}", None)
            server = response.get("server", response) if isinstance(response, dict) else None
            if isinstance(server, dict) and isinstance(server.get("ipv4"), str) and server["ipv4"]:
                return Machine(
                    id=str(server.get("id", machine_id)),
                    role=role,
                    ip=server["ipv4"],
                    observed_at=_now(),
                    run_tag=run_tag,
                    created_at=str(server["created"])
                    if server.get("created") is not None
                    else None,
                    hourly_cost_cents=server.get("costPerHr")
                    if isinstance(server.get("costPerHr"), int)
                    else None,
                )
            self._sleep(CREATE_POLL_SECONDS)
        raise LifecycleRefusal(
            f"machine_not_ready: {machine_id} did not receive an IP before timeout"
        )

    def create_run(self, *, run_tag: str) -> RunManifest:
        self.preflight(run_tag=run_tag)
        existing = self._existing_for_run(run_tag)
        if existing:
            raise LifecycleRefusal(
                "resource_ceiling_exhausted: run tag already owns resources; "
                "clean it before retrying"
            )
        # This is deliberately before the first create: no partial run can exceed the ceiling.
        if len(existing) + RESOURCE_CEILING > RESOURCE_CEILING:
            raise LifecycleRefusal("resource_ceiling_exhausted")
        _, ssh_key_id = self._account_and_key()
        created: list[Machine] = []
        try:
            for role in ROLES:
                response = self._request(
                    "POST",
                    "servers",
                    {
                        "server": {
                            "name": _machine_name(run_tag, role),
                            "hostID": HOST_ID,
                            "hostImageID": IMAGE_ID,
                            "sizeID": SIZE_ID,
                            "regionID": REGION_ID,
                            "sshKeys": [ssh_key_id],
                            "labels": {"run": run_tag, "role": role},
                        }
                    },
                )
                if not isinstance(response, dict) or response.get("id") is None:
                    raise LifecycleRefusal(
                        "create_response_invalid: BitLaunch did not return a server id"
                    )
                created.append(self._wait_for_machine(str(response["id"]), role, run_tag))
        except Exception:
            for machine in created:
                self._request("DELETE", f"servers/{machine.id}", None)
            raise
        return RunManifest(
            run_tag=run_tag,
            observed_at=_now(),
            machines=tuple(created),
            resource_ceiling=RESOURCE_CEILING,
        )

    def cleanup_run(self, *, run_tag: str) -> list[str]:
        policy = RunTagDestructionPolicy(run_tag)
        deleted: list[str] = []
        for machine in self._existing_for_run(run_tag):
            if policy.allows(machine):
                self._request("DELETE", f"servers/{machine.id}", None)
                deleted.append(machine.id)
        return deleted

    def sweep_expired(self, *, now: datetime, ttl: timedelta) -> list[str]:
        servers = self._request("GET", "servers", None)
        if not isinstance(servers, list):
            raise LifecycleRefusal("inventory_unusable: BitLaunch server response is malformed")
        machines = [
            machine for server in servers if (machine := _machine_from_server(server)) is not None
        ]
        deleted: list[str] = []
        for machine in select_expired_run_machines(machines, now=now, ttl=ttl):
            if machine.run_tag is not None and RunTagDestructionPolicy(machine.run_tag).allows(
                machine
            ):
                self._request("DELETE", f"servers/{machine.id}", None)
                deleted.append(machine.id)
        return deleted


def _live_lifecycle(env_file: Path | None, ssh_key_name: str) -> BitLaunchLifecycle:
    key = _api_key(env_file)

    def request(method: str, path: str, body: dict[str, Any] | None = None) -> object:
        return _request(key, method, path, body)

    return BitLaunchLifecycle(request, ssh_key_name=ssh_key_name)


def _write_manifest(path: Path, manifest: RunManifest) -> None:
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--ssh-key-name", default=DEFAULT_SSH_KEY_NAME)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "create", "cleanup"):
        item = sub.add_parser(command)
        item.add_argument("--run-tag", required=True)
    sub.choices["create"].add_argument("--manifest", type=Path, required=True)
    sweep = sub.add_parser("sweep")
    sweep.add_argument("--ttl-hours", type=int, required=True)
    args = parser.parse_args()
    lifecycle = _live_lifecycle(args.env_file, args.ssh_key_name)
    try:
        if args.command == "preflight":
            lifecycle.preflight(run_tag=args.run_tag)
            print(json.dumps({"status": "ready", "run_tag": args.run_tag}))
        elif args.command == "create":
            manifest = lifecycle.create_run(run_tag=args.run_tag)
            _write_manifest(args.manifest, manifest)
            print(manifest.to_json())
        elif args.command == "cleanup":
            print(json.dumps({"deleted_ids": lifecycle.cleanup_run(run_tag=args.run_tag)}))
        else:
            now = datetime.now(UTC)
            print(
                json.dumps(
                    {
                        "deleted_ids": lifecycle.sweep_expired(
                            now=now, ttl=timedelta(hours=args.ttl_hours)
                        )
                    }
                )
            )
    except LifecycleRefusal as exc:
        print(f"refused: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
