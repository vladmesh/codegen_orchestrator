"""A signed GitHub workflow completion wakes the PR/CI sweep; nothing else it sends does."""

import asyncio
import hashlib
import hmac
import json
import os
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request
from structlog.testing import capture_logs
from wake_doubles import NativeRedis

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from src.config import get_settings
from src.routers.github_webhooks import MAX_DELIVERY_BYTES, receive_github_webhook

WAKE = ("wake", SchedulerWakeTarget.PR_CI)
MALFORMED = "Malformed webhook delivery"


def secret() -> str:
    return get_settings().github_webhook_secret.get_secret_value()


def sign(body: bytes, key: str | None = None) -> str:
    digest = hmac.new((key or secret()).encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def delivery(action: str = "completed", conclusion: str | None = "success", **run) -> dict:
    return {
        "action": action,
        "workflow_run": {
            "id": 30433642,
            "name": "CI",
            "head_branch": "story/other-project",
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": conclusion,
            "pull_requests": [{"number": 999, "head": {"ref": "story/other-project"}}],
            "url": "https://api.github.com/repos/elsewhere/repo/actions/runs/30433642",
            **run,
        },
        "repository": {"full_name": "elsewhere/repo"},
        "installation": {"id": 1},
    }


def encode(payload) -> bytes:
    return json.dumps(payload).encode()


def headers(
    body: bytes,
    *,
    event: str | None = "workflow_run",
    content_type: str | None = "application/json",
    signatures: list[str] | None = None,
) -> list[tuple[str, str]]:
    pairs = []
    if content_type is not None:
        pairs.append(("content-type", content_type))
    if event is not None:
        pairs.append(("x-github-event", event))
    for signature in [sign(body)] if signatures is None else signatures:
        pairs.append(("x-hub-signature-256", signature))
    return pairs


def make_request(body: bytes, pairs: list[tuple[str, str]], chunk: int | None = None) -> Request:
    size = chunk or max(len(body), 1)
    parts = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
    messages = iter(
        {"type": "http.request", "body": part, "more_body": index < len(parts) - 1}
        for index, part in enumerate(parts)
    )

    async def receive():
        return next(messages)

    return Request(http_scope(pairs), receive)


def http_scope(pairs: list[tuple[str, str]]) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/webhooks/github",
        "headers": [(name.encode(), value.encode("latin-1")) for name, value in pairs],
    }


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def native(journal) -> NativeRedis:
    return NativeRedis(journal)


@pytest.fixture
def redis(native) -> SimpleNamespace:
    return SimpleNamespace(redis=native)


async def deliver(redis, body: bytes, pairs: list[tuple[str, str]] | None = None, **request):
    pairs = headers(body) if pairs is None else pairs
    return await receive_github_webhook(make_request(body, pairs, **request), redis=redis)


async def refused(redis, body: bytes, pairs: list[tuple[str, str]] | None = None, **request):
    with pytest.raises(HTTPException) as refusal:
        await deliver(redis, body, pairs, **request)
    return refusal.value


def assert_safe(logs: list, *needles: str) -> None:
    rendered = repr(logs)
    for needle in (secret(), *needles):
        assert needle not in rendered


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "timed_out", None])
async def test_a_signed_completion_publishes_one_hint_for_any_conclusion(
    journal, redis, conclusion
):
    with capture_logs() as logs:
        response = await deliver(redis, encode(delivery(conclusion=conclusion)))

    assert response.status_code == 204
    assert response.body == b""
    assert journal == [WAKE]
    [published] = [log for log in logs if log["event"] == "pr_ci_wake_published"]
    assert published == {
        "event": "pr_ci_wake_published",
        "log_level": "info",
        "source": "github_workflow_run",
        "receivers": 1,
    }


async def test_the_signature_covers_the_exact_bytes_including_whitespace_and_unicode(
    journal, redis
):
    body = (
        '{ "action" :"completed",\n\t"workflow_run": {"id": 7, "status": "completed",'
        ' "name": "Сборка ✓ \\u00e9"},  "sender": {"login": "测试"} }\r\n  '
    ).encode()
    reserialized = encode(json.loads(body))
    assert reserialized != body

    assert (await deliver(redis, body)).status_code == 204
    assert journal == [WAKE]

    refusal = await refused(redis, reserialized, headers(reserialized, signatures=[sign(body)]))
    assert refusal.status_code == 403
    assert journal == [WAKE]


async def test_a_body_modified_after_signing_is_refused(journal, redis):
    body = encode(delivery())
    tampered = body.replace(b"30433642", b"30433643")

    with capture_logs() as logs:
        refusal = await refused(redis, tampered, headers(tampered, signatures=[sign(body)]))

    assert (refusal.status_code, refusal.detail) == (403, "Invalid webhook signature")
    assert journal == []
    assert logs == [
        {
            "event": "github_webhook_refused",
            "log_level": "warning",
            "reason": "signature_mismatch",
            "status_code": 403,
        }
    ]


BODY = encode(delivery())
DIGEST = hmac.new(b"x", BODY, hashlib.sha256).hexdigest()


@pytest.mark.parametrize(
    ("signatures", "reason"),
    [
        ([], "signature_missing"),
        ([sign(BODY), sign(BODY)], "signature_ambiguous"),
        ([f"sha1={DIGEST[:40]}"], "signature_malformed"),
        (["sha256="], "signature_malformed"),
        ([f"sha256={DIGEST[:63]}"], "signature_malformed"),
        ([f"sha256={DIGEST}0"], "signature_malformed"),
        ([f"sha256={'z' * 64}"], "signature_malformed"),
        ([f"SHA256={DIGEST}"], "signature_malformed"),
        ([f"sha256={DIGEST},sha256={DIGEST}"], "signature_malformed"),
        ([f"sha256={DIGEST}"], "signature_mismatch"),
        ([sign(BODY, os.environ["INTERNAL_API_KEY"])], "signature_mismatch"),
        ([sign(BODY, os.environ["LK_JWT_SECRET"])], "signature_mismatch"),
    ],
    ids=lambda value: value if isinstance(value, str) else f"{len(value)}-headers",
)
async def test_a_missing_malformed_ambiguous_or_wrong_signature_is_refused(
    journal, redis, signatures, reason
):
    with capture_logs() as logs:
        refusal = await refused(redis, BODY, headers(BODY, signatures=signatures))

    assert (refusal.status_code, refusal.detail) == (403, "Invalid webhook signature")
    assert journal == []
    assert [(log["event"], log["reason"]) for log in logs] == [("github_webhook_refused", reason)]
    assert_safe(logs, DIGEST, "30433642", "elsewhere")


async def test_the_signature_is_checked_before_the_media_type_and_json(journal, redis):
    body = b"not json"

    refusal = await refused(
        redis, body, headers(body, content_type="text/plain", signatures=[f"sha256={DIGEST}"])
    )

    assert refusal.status_code == 403
    assert journal == []


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", 'Application/JSON;Charset="UTF-8"'],
)
async def test_a_json_media_type_with_an_optional_utf8_charset_is_accepted(
    journal, redis, content_type
):
    body = encode(delivery())

    assert (await deliver(redis, body, headers(body, content_type=content_type))).status_code == 204
    assert journal == [WAKE]


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "application/json; charset=latin-1",
        "application/json; boundary=x",
        "application/json; charset=utf-8; boundary=x",
        "application/jsonp",
    ],
)
async def test_a_signed_delivery_of_another_media_type_is_refused(journal, redis, content_type):
    body = encode(delivery())

    with capture_logs() as logs:
        refusal = await refused(redis, body, headers(body, content_type=content_type))

    assert (refusal.status_code, refusal.detail) == (415, "Webhook delivery must be JSON")
    assert journal == []
    assert [log["reason"] for log in logs] == ["unsupported_media_type"]


async def test_duplicate_content_type_headers_are_refused(journal, redis):
    body = encode(delivery())
    pairs = [*headers(body), ("content-type", "application/json")]

    assert (await refused(redis, body, pairs)).status_code == 415
    assert journal == []


def _completed_run(**changes) -> dict:
    payload = delivery()
    payload["workflow_run"].update(changes)
    return payload


@pytest.mark.parametrize(
    ("event", "body", "reason"),
    [
        ("workflow_run", b"{not json", "payload_not_json"),
        ("workflow_run", b"", "payload_not_json"),
        ("workflow_run", b'{"action": "completed", "x": "\xff"}', "payload_not_json"),
        ("workflow_run", b"[" * 100_000 + b"]" * 100_000, "payload_not_json"),
        ("workflow_run", b'["completed"]', "payload_not_object"),
        ("ping", b'"zen"', "payload_not_object"),
        ("push", b"null", "payload_not_object"),
        ("workflow_run", encode({"workflow_run": delivery()["workflow_run"]}), "payload_invalid"),
        ("workflow_run", encode({**delivery(), "action": 5}), "payload_invalid"),
        ("workflow_run", encode({"action": "completed"}), "payload_invalid"),
        ("workflow_run", encode({"action": "completed", "workflow_run": None}), "payload_invalid"),
        ("workflow_run", encode({"action": "completed", "workflow_run": []}), "payload_invalid"),
        ("workflow_run", encode(_completed_run(id=0)), "payload_invalid"),
        ("workflow_run", encode(_completed_run(id=-3)), "payload_invalid"),
        ("workflow_run", encode(_completed_run(id="30433642")), "payload_invalid"),
        ("workflow_run", encode(_completed_run(id=True)), "payload_invalid"),
        ("workflow_run", encode(_completed_run(id=1.0)), "payload_invalid"),
        ("workflow_run", encode(_completed_run(status="in_progress")), "payload_invalid"),
        ("workflow_run", encode(_completed_run(status=None)), "payload_invalid"),
    ],
)
async def test_an_authenticated_malformed_delivery_is_refused_without_echo(
    journal, redis, event, body, reason
):
    with capture_logs() as logs:
        refusal = await refused(redis, body, headers(body, event=event))

    assert (refusal.status_code, refusal.detail) == (400, MALFORMED)
    assert journal == []
    assert logs == [
        {
            "event": "github_webhook_refused",
            "log_level": "warning",
            "reason": reason,
            "status_code": 400,
        }
    ]


@pytest.mark.parametrize("pairs", [[], [("x-github-event", "workflow_run")] * 2])
async def test_a_missing_or_duplicate_event_header_is_refused(journal, redis, pairs):
    body = encode(delivery())

    refusal = await refused(redis, body, [*headers(body, event=None), *pairs])

    assert (refusal.status_code, refusal.detail) == (400, MALFORMED)
    assert journal == []


@pytest.mark.parametrize(
    ("event", "payload", "kind"),
    [
        ("ping", {"zen": "Keep it logically awesome.", "hook_id": 1}, "ping"),
        ("push", {"ref": "refs/heads/main"}, "unsupported_event"),
        ("check_suite", {"action": "completed", "check_suite": {"id": 1}}, "unsupported_event"),
        ("workflow_run", delivery(action="requested", status="queued"), "workflow_run_action"),
        (
            "workflow_run",
            delivery(action="in_progress", status="in_progress"),
            "workflow_run_action",
        ),
        ("workflow_run", {"action": "requested", "workflow_run": None}, "workflow_run_action"),
    ],
)
async def test_a_signed_ping_or_ignored_delivery_is_acknowledged_without_a_hint(
    journal, redis, event, payload, kind
):
    body = encode(payload)

    with capture_logs() as logs:
        response = await deliver(redis, body, headers(body, event=event))

    assert response.status_code == 204
    assert journal == []
    assert logs == [{"event": "github_webhook_ignored", "log_level": "info", "event_kind": kind}]


async def test_a_replayed_or_reordered_delivery_publishes_again(journal, redis):
    completed = encode(delivery())
    earlier = encode(delivery(action="in_progress", status="in_progress"))

    for body in (completed, completed, earlier, completed):
        assert (await deliver(redis, body)).status_code == 204

    assert journal == [WAKE] * 3


def padded(size: int) -> bytes:
    body = encode(delivery())
    return body + b" " * (size - len(body))


async def test_a_delivery_of_exactly_the_bound_is_accepted(journal, redis):
    body = padded(MAX_DELIVERY_BYTES)

    assert (await deliver(redis, body, chunk=65536)).status_code == 204
    assert journal == [WAKE]


@pytest.mark.parametrize("declared", [None, "10", str(MAX_DELIVERY_BYTES)])
async def test_a_stream_past_the_bound_is_refused_whatever_its_length_says(
    journal, redis, declared
):
    body = padded(MAX_DELIVERY_BYTES + 1)
    pairs = headers(body)
    if declared is not None:
        pairs.append(("content-length", declared))

    with capture_logs() as logs:
        refusal = await refused(redis, body, pairs, chunk=65536)

    assert (refusal.status_code, refusal.detail) == (413, "Webhook delivery too large")
    assert journal == []
    assert [log["reason"] for log in logs] == ["body_too_large"]


async def test_an_endless_stream_is_refused_after_reading_only_the_bound(journal, redis):
    chunk = b" " * 65536
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    request = Request(http_scope([("content-type", "application/json")]), receive)
    with pytest.raises(HTTPException) as refusal:
        await receive_github_webhook(request, redis=redis)

    assert refusal.value.status_code == 413
    assert reads == MAX_DELIVERY_BYTES // len(chunk) + 1
    assert journal == []


async def test_zero_receivers_is_an_acknowledged_hint(journal, redis, native):
    native.receivers = 0

    assert (await deliver(redis, encode(delivery()))).status_code == 204
    assert journal == [WAKE]


@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, failure):
    native.failure = failure

    with capture_logs() as logs:
        response = await deliver(redis, encode(delivery()))

    assert response.status_code == 204
    assert journal == []
    assert logs == [
        {
            "event": "pr_ci_wake_failed",
            "log_level": "warning",
            "source": "github_workflow_run",
            "error_type": type(failure).__name__,
        }
    ]


async def test_cancellation_during_publish_propagates(journal, redis, native):
    native.hold = asyncio.Event()

    request = asyncio.create_task(deliver(redis, encode(delivery())))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == []


async def test_cancellation_while_reading_the_body_propagates(journal, redis):
    reading = asyncio.Event()

    async def receive():
        reading.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        receive_github_webhook(Request(http_scope(headers(b"{}")), receive), redis=redis)
    )
    await asyncio.wait_for(reading.wait(), 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert journal == []
