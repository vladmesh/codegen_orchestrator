"""Narrow client for the generated product's core jobs contract.

The same shape as `product_settings.py`, one contract along: it calls only the
two documented endpoints of the released jobs core (`service-template`,
`docs/CONTRACTS.md`, "Core jobs v1"), takes the deployment capability as an
argument, puts it in a request header and nowhere else, and returns a bounded,
credential-safe outcome.

The caller names a *behaviour*, never a module, a queue, a container or a
transport, and never holds the product's capability itself — the QA runtime on
the management host resolves it and passes it in for the one call that needs
it. Reading evidence back carries no capability at all.

What comes back is deliberately not a verdict. `dispatch_status == dispatched`
records that the product's core published `job_fired`; the template's own
contract says in those words that it is *not* evidence that a provider consumed
the event or ran the behaviour. So every outcome this client produces carries
that sentence with it, and QA's judgement rests on the behaviour's own output.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Protocol

import httpx

#: Every generated jobs schema carries it, and the product refuses any other value.
_CONTRACT_VERSION = 1

#: The one header `POST /jobs/fire` authenticates with. It is deliberately
#: absent from the generated schemas and OpenAPI, and must never be logged, put
#: in a URL, or reach LLM-facing data. `POST /jobs/evidence` does not carry it.
_CAPABILITY_HEADER = "X-Jobs-Capability"  # noqa: S105

JOBS_REQUEST_TIMEOUT = 30.0

#: What a caller must be told about `dispatched`, wherever a dispatch record is
#: reported. Kept as one constant so an executor, a prompt and a trace line
#: cannot end up saying three different things about it.
DISPATCH_IS_NOT_PROOF = (
    "dispatch_status records only that the product's core published job_fired. "
    "It is not evidence that any provider consumed the event or ran the behaviour. "
    "Judge this check on the behaviour's own output."
)


class JobCallFailure(StrEnum):
    """Why a call to the product's jobs core produced no usable evidence.

    A closed set, so a QA-visible failure is a named outcome rather than a
    stack trace: the two refusals that are the product's own contract keep
    their own names, and everything else is separated by whether the product
    answered at all.
    """

    #: 404 on a fire: the product declares no such behaviour in its manifest.
    NAME_NOT_DECLARED = "name_not_declared"
    #: 422 on a fire: the arguments do not satisfy the declared schema.
    ARGUMENTS_REJECTED = "arguments_rejected"
    #: 404 on an evidence read: this identity has no recorded command.
    NO_COMMAND_RECORDED = "no_command_recorded"
    #: Any other refusal by the product.
    REJECTED = "rejected"
    #: The product was not reached at all.
    TRANSPORT = "transport"
    #: The product answered with something that is not a `JobCommand`.
    MALFORMED_ANSWER = "malformed_answer"


@dataclass(frozen=True)
class JobCommandEvidence:
    """One recorded command, as the product reports it. No capability, no URL."""

    command_id: str
    name: str
    arguments: Any
    fired_by_product: str
    fired_by_run: str
    dispatch_status: str
    accepted_at: str
    dispatched_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "name": self.name,
            "arguments": self.arguments,
            "fired_by_product": self.fired_by_product,
            "fired_by_run": self.fired_by_run,
            "dispatch_status": self.dispatch_status,
            "accepted_at": self.accepted_at,
            "dispatched_at": self.dispatched_at,
        }


@dataclass(frozen=True)
class JobCallOutcome:
    """Exactly one of: the recorded command, or why there is none."""

    command: JobCommandEvidence | None = None
    failure: JobCallFailure | None = None


class _RequestClient(Protocol):
    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response: ...


class GeneratedServiceJobsClient:
    """Call only the generated product's documented jobs endpoints."""

    def __init__(self, base_url: str, *, transport: _RequestClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[_RequestClient]:
        """Use the injected transport, or own one for the length of one call."""
        if self._transport is not None:
            yield self._transport
            return
        async with httpx.AsyncClient(
            timeout=JOBS_REQUEST_TIMEOUT, follow_redirects=False
        ) as client:
            yield client

    async def fire(
        self,
        *,
        command_id: str,
        name: str,
        arguments: Any,
        fired_by_product: str,
        fired_by_run: str,
        capability: str,
    ) -> JobCallOutcome:
        """Fire a named behaviour under a caller-owned command identity.

        Identity is `(fired_by_product, command_id)`, and the product bounds
        execution on it: a repeat of the same identity returns the recorded
        evidence and emits nothing. That is what makes a retry of this call
        safe, so the identity is the caller's to choose and to reuse.
        """
        payload = {
            "contract_version": _CONTRACT_VERSION,
            "command_id": command_id,
            "name": name,
            "arguments": arguments,
            "fired_by_product": fired_by_product,
            "fired_by_run": fired_by_run,
        }
        return await self._call(
            "/jobs/fire", payload, headers={_CAPABILITY_HEADER: capability}, fired=True
        )

    async def evidence(self, *, command_id: str, fired_by_product: str) -> JobCallOutcome:
        """Read back what the product recorded for one command identity."""
        payload = {
            "contract_version": _CONTRACT_VERSION,
            "command_id": command_id,
            "fired_by_product": fired_by_product,
        }
        return await self._call("/jobs/evidence", payload, headers=None, fired=False)

    async def _call(
        self, path: str, payload: dict, *, headers: dict[str, str] | None, fired: bool
    ) -> JobCallOutcome:
        try:
            async with self._client() as client:
                response = await client.request(
                    "POST", f"{self._base_url}{path}", headers=headers, json=payload
                )
        except httpx.HTTPError:
            return JobCallOutcome(failure=JobCallFailure.TRANSPORT)
        if not response.is_success:
            return JobCallOutcome(failure=_refusal(response.status_code, fired=fired))
        return _read_command(response)


def _refusal(status_code: int, *, fired: bool) -> JobCallFailure:
    """Two of the product's refusals are its own contract, and mean their own thing."""
    if status_code == HTTPStatus.NOT_FOUND:
        return JobCallFailure.NAME_NOT_DECLARED if fired else JobCallFailure.NO_COMMAND_RECORDED
    if fired and status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
        return JobCallFailure.ARGUMENTS_REJECTED
    return JobCallFailure.REJECTED


_COMMAND_FIELDS = (
    "command_id",
    "name",
    "fired_by_product",
    "fired_by_run",
    "dispatch_status",
    "accepted_at",
)


def _read_command(response: httpx.Response) -> JobCallOutcome:
    """Accept only an answer shaped like the released `JobCommand`."""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return JobCallOutcome(failure=JobCallFailure.MALFORMED_ANSWER)
    if not isinstance(payload, dict):
        return JobCallOutcome(failure=JobCallFailure.MALFORMED_ANSWER)
    for field in _COMMAND_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            return JobCallOutcome(failure=JobCallFailure.MALFORMED_ANSWER)
    dispatched_at = payload.get("dispatched_at")
    if dispatched_at is not None and not isinstance(dispatched_at, str):
        return JobCallOutcome(failure=JobCallFailure.MALFORMED_ANSWER)
    return JobCallOutcome(
        command=JobCommandEvidence(
            command_id=payload["command_id"],
            name=payload["name"],
            arguments=payload.get("arguments"),
            fired_by_product=payload["fired_by_product"],
            fired_by_run=payload["fired_by_run"],
            dispatch_status=payload["dispatch_status"],
            accepted_at=payload["accepted_at"],
            dispatched_at=dispatched_at,
        )
    )
