"""ConfigStore behavior, driven through a recording httpx transport.

The tests drive the shared internal-API transport so snapshot, source-failure,
fail-closed, bounded-stale, and internal-header behavior stay covered together.
"""

import httpx
import pytest
from structlog.testing import capture_logs

from shared.config_store import BoundedStalePolicy, ConfigStore, ConfigStoreUnavailableError
from shared.log_config.correlation import clear_context, set_correlation_id

INTERNAL_KEY = "config-store-test-key"
TTL = 30


class _Responder:
    """Answers with the configured configs and keeps what was actually sent."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.json_body: dict | list | None = [{"key": "scheduler.interval", "value": 42}]
        self.error: Exception | None = None

    def serve(self, **values) -> None:
        self.json_body = [{"key": key, "value": value} for key, value in values.items()]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, json=self.json_body)

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request reached the transport"
        return self.requests[-1]


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_correlation_context():
    clear_context()
    yield
    clear_context()


@pytest.fixture
def responder(monkeypatch) -> _Responder:
    rec = _Responder()
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(rec), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.AsyncClient", factory)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    return rec


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr("shared.config_store.time.monotonic", fake)
    return fake


@pytest.fixture
def store(responder, clock) -> ConfigStore:
    return ConfigStore("http://test:8000", cache_ttl=TTL)


def _bounded_store(
    *,
    key: str = "scheduler.interval",
    max_age_seconds: float = 60,
) -> ConfigStore:
    return ConfigStore(
        "http://test:8000",
        cache_ttl=TTL,
        stale_policies={key: BoundedStalePolicy(max_age_seconds=max_age_seconds)},
    )


async def _fail_refresh(store: ConfigStore) -> None:
    with pytest.raises(ConfigStoreUnavailableError):
        await store.refresh()


class TestInternalAPIHeaders:
    async def test_a_refresh_carries_both_internal_api_headers(self, store, responder):
        set_correlation_id("corr-9")
        await store.refresh()

        sent = responder.last
        assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
        assert sent.headers["X-Correlation-ID"] == "corr-9"

    async def test_a_startup_refresh_with_no_context_is_still_labelled(self, store, responder):
        """The scheduler reads its config before anything binds a context."""
        await store.refresh()
        assert responder.last.headers["X-Correlation-ID"]


class TestPolicies:
    @pytest.mark.parametrize("max_age_seconds", [0, -1])
    def test_requires_a_positive_age_bound(self, max_age_seconds):
        with pytest.raises(ValueError, match="greater than zero"):
            BoundedStalePolicy(max_age_seconds=max_age_seconds)

    @pytest.mark.parametrize("cache_ttl", [0, -1])
    def test_requires_a_positive_ttl(self, responder, cache_ttl):
        with pytest.raises(ValueError, match="greater than zero"):
            ConfigStore("http://test:8000", cache_ttl=cache_ttl)


class TestRefresh:
    async def test_one_request_reads_every_key(self, store, responder):
        responder.serve(**{"scheduler.interval": 42, "health.threshold": 90.5})

        await store.refresh()

        assert responder.call_count == 1
        assert responder.last.method == "GET"
        assert responder.last.url.path == "/api/system-configs/"
        assert store.get("scheduler.interval") == 42
        assert store.get("health.threshold") == 90.5

    async def test_reads_never_reach_the_transport(self, store, responder, clock):
        responder.serve(**{"a.int": 30, "b.float": 90.5, "c.str": "main"})
        await store.refresh()

        for _ in range(3):
            clock.now += 5
            assert store.get_int("a.int") == 30
            assert store.get_float("b.float") == 90.5
            assert store.get("c.str") == "main"
            assert store.get("absent", default=7) == 7
            with pytest.raises(KeyError):
                store.get("absent")
        store.validate_required(["a.int", "b.float", "c.str"])

        assert responder.call_count == 1

    async def test_a_later_refresh_replaces_the_snapshot(self, store, responder):
        await store.refresh()
        responder.serve(**{"scheduler.other": 1})

        await store.refresh()

        assert store.get("scheduler.other") == 1
        assert store.get("scheduler.interval", default=None) is None

    @pytest.mark.parametrize(
        ("break_source", "reason"),
        [
            (lambda r: setattr(r, "error", httpx.ConnectError("refused")), "request failed"),
            (lambda r: setattr(r, "status_code", 503), "HTTP 503"),
            (lambda r: setattr(r, "json_body", {}), "invalid response body"),
            (lambda r: setattr(r, "json_body", [{"key": "k"}]), "invalid response body"),
        ],
    )
    async def test_a_failed_refresh_raises_and_keeps_the_snapshot(
        self, store, responder, break_source, reason
    ):
        await store.refresh()
        break_source(responder)

        with pytest.raises(ConfigStoreUnavailableError, match=reason):
            await store.refresh()

        assert store.get("scheduler.interval") == 42

    async def test_refresh_forever_retries_after_a_failure(self, store, responder):
        class Stop(Exception):
            pass

        waits: list[float] = []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                raise Stop
            responder.error = None if len(waits) == 2 else httpx.ConnectError("refused")

        with pytest.raises(Stop):
            await store.refresh_forever(sleep=sleep)

        assert waits == [TTL / 2] * 3
        assert responder.call_count == 2
        assert store.get("scheduler.interval") == 42


class TestGet:
    async def test_get_returns_value(self, store):
        await store.refresh()
        assert store.get("scheduler.interval") == 42

    async def test_get_raises_keyerror_when_not_found(self, store):
        await store.refresh()
        with pytest.raises(KeyError, match="not found"):
            store.get("nonexistent")

    async def test_get_returns_default_when_not_found(self, store):
        await store.refresh()
        assert store.get("nonexistent", default=99) == 99

    def test_reads_before_the_first_snapshot_fail_closed(self, store, responder):
        with pytest.raises(ConfigStoreUnavailableError, match="no snapshot yet"):
            store.get("scheduler.interval", default=1)
        assert responder.call_count == 0

    async def test_reads_after_a_failed_first_refresh_fail_closed_with_its_reason(
        self, responder, clock
    ):
        store = _bounded_store(key="scheduler.interval")
        responder.status_code = 503
        await _fail_refresh(store)

        with pytest.raises(ConfigStoreUnavailableError, match="503"):
            store.get("scheduler.interval")

    async def test_a_failed_refresh_within_the_ttl_still_serves_the_snapshot(
        self, store, responder, clock
    ):
        await store.refresh()
        clock.now += TTL / 2
        responder.error = httpx.ConnectError("connection refused")
        await _fail_refresh(store)

        assert store.get("scheduler.interval") == 42

    async def test_unclassified_key_fails_closed_past_the_ttl(self, store, responder, clock):
        await store.refresh()
        clock.now += TTL
        responder.error = httpx.ConnectError("connection refused")
        await _fail_refresh(store)

        with pytest.raises(ConfigStoreUnavailableError, match="unavailable.*request failed"):
            store.get("scheduler.interval")
        with pytest.raises(ConfigStoreUnavailableError):
            store.get("absent", default=1)

    async def test_a_snapshot_nobody_refreshes_fails_closed_past_the_ttl(self, store, clock):
        await store.refresh()
        clock.now += TTL

        with pytest.raises(ConfigStoreUnavailableError, match="no refresh within"):
            store.get("scheduler.interval")

    async def test_a_successful_refresh_ends_the_outage(self, store, responder, clock):
        await store.refresh()
        clock.now += TTL
        responder.status_code = 503
        await _fail_refresh(store)
        responder.status_code = 200

        await store.refresh()

        assert store.get("scheduler.interval") == 42

    @pytest.mark.parametrize(
        "break_source",
        [
            lambda r: setattr(r, "error", httpx.ConnectError("connection refused")),
            lambda r: setattr(r, "status_code", 503),
            lambda r: setattr(r, "json_body", {}),
        ],
    )
    async def test_bounded_key_uses_last_known_value_past_the_ttl(
        self, responder, clock, break_source
    ):
        store = _bounded_store(key="scheduler.interval")
        await store.refresh()
        clock.now += TTL
        break_source(responder)
        await _fail_refresh(store)

        with capture_logs() as logs:
            assert store.get("scheduler.interval") == 42

        assert [entry["event"] for entry in logs] == [
            "config_store_source_unavailable_using_bounded_stale"
        ]

    async def test_bounded_key_accepts_value_at_exact_stale_age_limit(self, responder, clock):
        store = _bounded_store(key="scheduler.interval", max_age_seconds=60)
        await store.refresh()
        clock.now += 60
        responder.status_code = 503
        await _fail_refresh(store)

        assert store.get("scheduler.interval") == 42

    async def test_bounded_key_fails_after_max_stale_age_with_an_error_log(self, responder, clock):
        store = _bounded_store(key="scheduler.interval", max_age_seconds=60)
        await store.refresh()
        clock.now += 61
        responder.error = httpx.ConnectError("connection refused")
        await _fail_refresh(store)

        with capture_logs() as logs, pytest.raises(ConfigStoreUnavailableError):
            store.get("scheduler.interval")

        [expired] = [e for e in logs if e["event"].endswith("_stale_expired")]
        assert expired["log_level"] == "error"
        assert expired["key"] == "scheduler.interval"

    async def test_bounded_key_absent_from_the_last_snapshot_fails_closed(self, responder, clock):
        store = _bounded_store(key="key1")
        await store.refresh()
        clock.now += TTL
        responder.status_code = 503
        await _fail_refresh(store)

        with pytest.raises(ConfigStoreUnavailableError):
            store.get("key1", default=5)

    async def test_get_still_raises_keyerror_for_a_deleted_key_with_a_last_known_value(
        self, responder, clock
    ):
        store = _bounded_store(key="key1")
        responder.serve(key1=1)
        await store.refresh()
        responder.serve()
        await store.refresh()

        with pytest.raises(KeyError, match="not found"):
            store.get("key1")


class TestTypedGetters:
    async def test_get_int(self, store, responder):
        responder.serve(**{"scheduler.interval": 30})
        await store.refresh()
        result = store.get_int("scheduler.interval")
        assert result == 30
        assert isinstance(result, int)

    async def test_get_int_coerces_float(self, store, responder):
        responder.serve(**{"scheduler.interval": 30.0})
        await store.refresh()
        result = store.get_int("scheduler.interval")
        assert result == 30
        assert isinstance(result, int)

    async def test_get_float(self, store, responder):
        responder.serve(**{"health.threshold": 90.5})
        await store.refresh()
        result = store.get_float("health.threshold")
        assert result == 90.5
        assert isinstance(result, float)

    async def test_get_int_raises_on_missing_without_default(self, store):
        await store.refresh()
        with pytest.raises(KeyError):
            store.get_int("missing")

    async def test_get_int_returns_default(self, store):
        await store.refresh()
        assert store.get_int("missing", default=5) == 5


class TestValidateRequired:
    async def test_validate_passes_when_all_present(self, store, responder):
        responder.serve(key1=1, key2=2)
        await store.refresh()
        store.validate_required(["key1", "key2"])

    async def test_validate_lists_all_missing(self, store):
        await store.refresh()
        with pytest.raises(RuntimeError, match="Missing required system configs: key1, key2"):
            store.validate_required(["key1", "key2"])

    async def test_validate_propagates_api_unavailability(self, store, responder):
        responder.error = httpx.ConnectError("connection refused")
        await _fail_refresh(store)
        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.validate_required(["key1", "key2"])
