"""ConfigStore behaviour, driven through a recording httpx transport.

The store used to be mocked at `httpx.get`. It reads the internal API now, so
the tests drive the shared transport instead: the same cache and fallback
behaviour, plus proof that a config read carries the two internal API headers.
"""

import time

import httpx
import pytest

from shared.config_store import ConfigStore, ConfigStoreUnavailableError
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

    def test_a_category_read_carries_both_internal_api_headers(self, store, responder):
        responder.json_body = [{"key": "sched.a", "value": 1}]
        set_correlation_id("corr-9")

        store.get_category("scheduler")

        sent = responder.last
        assert sent.headers["X-Internal-Key"] == INTERNAL_KEY
        assert sent.headers["X-Correlation-ID"] == "corr-9"
        assert sent.url.path == "/api/system-configs/"
        assert sent.url.params["category"] == "scheduler"


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

    def test_get_uses_stale_cache_on_network_error(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        time.sleep(0.01)
        responder.error = httpx.ConnectError("connection refused")
        assert store.get("key1") == 42

    def test_get_distinguishes_unavailable_api_from_missing_config(self, store, responder):
        responder.error = httpx.ConnectError("connection refused")
        with pytest.raises(ConfigStoreUnavailableError, match="unavailable"):
            store.get("scheduler.interval")

    def test_get_treats_an_empty_success_response_as_api_unavailability(self, store, responder):
        responder.json_body = {}
        with pytest.raises(ConfigStoreUnavailableError, match="invalid response"):
            store.get("scheduler.interval")

    def test_get_uses_last_known_value_on_server_error(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        time.sleep(0.01)
        responder.status_code = 503
        assert store.get("key1") == 42

    def test_get_uses_last_known_value_on_broken_response_body(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        time.sleep(0.01)
        responder.json_body = {}
        assert store.get("key1") == 42

    def test_get_raises_unavailable_on_server_error_without_last_known_value(
        self, store, responder
    ):
        responder.status_code = 503
        with pytest.raises(ConfigStoreUnavailableError, match="503"):
            store.get("key1")

    def test_get_still_raises_keyerror_for_a_deleted_key_with_a_last_known_value(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        store.get("key1")
        time.sleep(0.01)
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


class TestGetCategory:
    def test_get_category_returns_dict(self, store, responder):
        responder.json_body = [
            {"key": "sched.a", "value": 1},
            {"key": "sched.b", "value": 2},
        ]
        assert store.get_category("scheduler") == {"sched.a": 1, "sched.b": 2}

    def test_get_category_populates_cache(self, responder):
        store = ConfigStore("http://test:8000", cache_ttl=60)
        responder.json_body = [{"key": "sched.a", "value": 1}]
        store.get_category("scheduler")
        # Now individual get should use cache
        assert store.get("sched.a") == 1
        # Only 1 HTTP call total (the category call)
        assert responder.call_count == 1

    def test_get_category_returns_empty_on_error(self, store, responder):
        responder.error = httpx.ConnectError("connection refused")
        assert store.get_category("scheduler") == {}


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
