"""ConfigStore behavior, driven through a recording httpx transport.

The tests drive the shared internal-API transport so cache, source-failure,
fail-closed, bounded-stale, and internal-header behavior stay covered together.
"""

import time

import httpx
import pytest

from shared.config_store import BoundedStalePolicy, ConfigStore, ConfigStoreUnavailableError
from shared.log_config.correlation import clear_context, set_correlation_id

INTERNAL_KEY = "config-store-test-key"


class _Responder:
    """Answers with a queued response and keeps what was actually sent."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.json_body: dict | list | None = {"key": "test", "value": 42}
        self.error: Exception | None = None

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


@pytest.fixture(autouse=True)
def _clean_correlation_context():
    clear_context()
    yield
    clear_context()


@pytest.fixture
def responder(monkeypatch) -> _Responder:
    rec = _Responder()
    real_client = httpx.Client

    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(rec), **kwargs)

    monkeypatch.setattr("shared.clients.internal_api.httpx.Client", factory)
    monkeypatch.setenv("INTERNAL_API_KEY", INTERNAL_KEY)
    return rec


@pytest.fixture
def store(responder) -> ConfigStore:
    return ConfigStore("http://test:8000")


def _bounded_store(
    *,
    key: str = "scheduler.interval",
    max_age_seconds: float = 60,
) -> ConfigStore:
    return ConfigStore(
        "http://test:8000",
        cache_ttl=0,
        stale_policies={key: BoundedStalePolicy(max_age_seconds=max_age_seconds)},
    )


class TestInternalAPIHeaders:
    def test_a_config_read_carries_both_internal_api_headers(self, store, responder):
        set_correlation_id("corr-9")
        store.get("scheduler.interval")

        sent = responder.last
        assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
        assert sent.headers["X-Correlation-ID"] == "corr-9"
        assert sent.url.path == "/api/system-configs/scheduler.interval"

    def test_a_startup_read_with_no_context_is_still_labelled(self, store, responder):
        """The scheduler reads its config before anything binds a context."""
        store.get("scheduler.interval")
        assert responder.last.headers["X-Correlation-ID"]


class TestBoundedStalePolicy:
    @pytest.mark.parametrize("max_age_seconds", [0, -1])
    def test_requires_a_positive_age_bound(self, max_age_seconds):
        with pytest.raises(ValueError, match="greater than zero"):
            BoundedStalePolicy(max_age_seconds=max_age_seconds)


class TestGet:
    def test_get_returns_value(self, store):
        assert store.get("scheduler.interval") == 42

    def test_get_uses_cache(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=60)
        store.get("key1")
        store.get("key1")
        assert responder.call_count == 1

    def test_get_cache_expires(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        time.sleep(0.01)
        store.get("key1")
        assert responder.call_count == 2

    def test_get_raises_keyerror_when_not_found(self, store, responder):
        responder.status_code = 404
        with pytest.raises(KeyError, match="not found"):
            store.get("nonexistent")

    def test_get_returns_default_when_not_found(self, store, responder):
        responder.status_code = 404
        assert store.get("nonexistent", default=99) == 99

    def test_unclassified_key_fails_closed_even_with_last_known_value(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        responder.error = httpx.ConnectError("connection refused")

        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.get("key1")

    def test_get_distinguishes_unavailable_api_from_missing_config(self, store, responder):
        responder.error = httpx.ConnectError("connection refused")
        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.get("scheduler.interval")

    def test_get_treats_an_empty_success_response_as_api_unavailability(self, store, responder):
        responder.json_body = {}
        with pytest.raises(ConfigStoreUnavailableError, match="invalid response"):
            store.get("scheduler.interval")

    def test_bounded_key_uses_last_known_value_on_network_error(self, responder):
        store = _bounded_store(key="scheduler.interval")
        store.get("scheduler.interval")
        responder.error = httpx.ConnectError("connection refused")

        assert store.get("scheduler.interval") == 42

    def test_bounded_key_uses_last_known_value_on_server_error(self, responder):
        store = _bounded_store(key="scheduler.interval")
        store.get("scheduler.interval")
        responder.status_code = 503

        assert store.get("scheduler.interval") == 42

    def test_bounded_key_uses_last_known_value_on_broken_response_body(self, responder):
        store = _bounded_store(key="scheduler.interval")
        store.get("scheduler.interval")
        responder.json_body = {}

        assert store.get("scheduler.interval") == 42

    def test_bounded_key_fails_after_max_stale_age(self, responder, monkeypatch):
        clock = 100.0
        monkeypatch.setattr("shared.config_store.time.monotonic", lambda: clock)
        store = _bounded_store(
            key="scheduler.interval",
            max_age_seconds=60,
        )
        store.get("scheduler.interval")

        clock = 161.0
        responder.error = httpx.ConnectError("connection refused")

        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.get("scheduler.interval")

    def test_bounded_key_accepts_value_at_exact_stale_age_limit(self, responder, monkeypatch):
        clock = 100.0
        monkeypatch.setattr("shared.config_store.time.monotonic", lambda: clock)
        store = _bounded_store(
            key="scheduler.interval",
            max_age_seconds=60,
        )
        store.get("scheduler.interval")

        clock = 160.0
        responder.status_code = 503

        assert store.get("scheduler.interval") == 42

    def test_get_raises_unavailable_on_server_error_without_last_known_value(
        self, store, responder
    ):
        responder.status_code = 503
        with pytest.raises(ConfigStoreUnavailableError, match="503"):
            store.get("key1")

    def test_get_still_raises_keyerror_for_a_deleted_key_with_a_last_known_value(self, responder):
        store = _bounded_store(key="key1")
        store.get("key1")
        responder.status_code = 404

        with pytest.raises(KeyError, match="not found"):
            store.get("key1")


class TestTypedGetters:
    def test_get_int(self, store, responder):
        responder.json_body = {"key": "test", "value": 30}
        result = store.get_int("scheduler.interval")
        assert result == 30
        assert isinstance(result, int)

    def test_get_int_coerces_float(self, store, responder):
        responder.json_body = {"key": "test", "value": 30.0}
        result = store.get_int("scheduler.interval")
        assert result == 30
        assert isinstance(result, int)

    def test_get_float(self, store, responder):
        responder.json_body = {"key": "test", "value": 90.5}
        result = store.get_float("health.threshold")
        assert result == 90.5
        assert isinstance(result, float)

    def test_get_int_raises_on_missing_without_default(self, store, responder):
        responder.status_code = 404
        with pytest.raises(KeyError):
            store.get_int("missing")

    def test_get_int_returns_default(self, store, responder):
        responder.status_code = 404
        assert store.get_int("missing", default=5) == 5


class TestValidateRequired:
    def test_validate_passes_when_all_present(self, store, responder):
        responder.json_body = {"key": "test", "value": 1}
        store.validate_required(["key1", "key2"])

    def test_validate_raises_on_missing(self, store, responder):
        responder.status_code = 404
        with pytest.raises(RuntimeError, match="Missing required system configs"):
            store.validate_required(["key1", "key2"])

    def test_validate_lists_all_missing(self, store, responder):
        responder.status_code = 404
        with pytest.raises(RuntimeError, match="key1.*key2"):
            store.validate_required(["key1", "key2"])

    def test_validate_propagates_api_unavailability(self, store, responder):
        responder.error = httpx.ConnectError("connection refused")
        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.validate_required(["key1", "key2"])
