"""Unit tests for ConfigStore with mocked HTTP responses."""

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from shared.config_store import ConfigStore


def _mock_response(status_code: int, json_data: dict | list | None = None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


class TestGet:
    def test_get_returns_value(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 42, "key": "test"})
            assert store.get("scheduler.interval") == 42

    def test_get_uses_cache(self):
        store = ConfigStore("http://test:8000", cache_ttl=60)
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 42, "key": "test"})
            store.get("key1")
            store.get("key1")
            assert mock_get.call_count == 1

    def test_get_cache_expires(self):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 42, "key": "test"})
            store.get("key1")
            time.sleep(0.01)
            store.get("key1")
            assert mock_get.call_count == 2

    def test_get_raises_keyerror_when_not_found(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            with pytest.raises(KeyError, match="not found"):
                store.get("nonexistent")

    def test_get_returns_default_when_not_found(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            assert store.get("nonexistent", default=99) == 99

    def test_get_uses_stale_cache_on_network_error(self):
        store = ConfigStore("http://test:8000", cache_ttl=0)
        with patch("shared.config_store.httpx.get") as mock_get:
            # First call succeeds
            mock_get.return_value = _mock_response(200, {"value": 42, "key": "test"})
            store.get("key1")
            # Second call fails — should use stale cache
            time.sleep(0.01)
            mock_get.side_effect = httpx.ConnectError("connection refused")
            assert store.get("key1") == 42


class TestTypedGetters:
    def test_get_int(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 30, "key": "test"})
            result = store.get_int("scheduler.interval")
            assert result == 30
            assert isinstance(result, int)

    def test_get_int_coerces_float(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 30.0, "key": "test"})
            result = store.get_int("scheduler.interval")
            assert result == 30
            assert isinstance(result, int)

    def test_get_float(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 90.5, "key": "test"})
            result = store.get_float("health.threshold")
            assert result == 90.5
            assert isinstance(result, float)

    def test_get_int_raises_on_missing_without_default(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            with pytest.raises(KeyError):
                store.get_int("missing")

    def test_get_int_returns_default(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            assert store.get_int("missing", default=5) == 5


class TestGetCategory:
    def test_get_category_returns_dict(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(
                200,
                [
                    {"key": "sched.a", "value": 1},
                    {"key": "sched.b", "value": 2},
                ],
            )
            result = store.get_category("scheduler")
            assert result == {"sched.a": 1, "sched.b": 2}

    def test_get_category_populates_cache(self):
        store = ConfigStore("http://test:8000", cache_ttl=60)
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(
                200,
                [{"key": "sched.a", "value": 1}],
            )
            store.get_category("scheduler")
            # Now individual get should use cache
            assert store.get("sched.a") == 1
            # Only 1 HTTP call total (the category call)
            assert mock_get.call_count == 1

    def test_get_category_returns_empty_on_error(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.side_effect = httpx.ConnectError("connection refused")
            assert store.get_category("scheduler") == {}


class TestValidateRequired:
    def test_validate_passes_when_all_present(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(200, {"value": 1, "key": "test"})
            store.validate_required(["key1", "key2"])

    def test_validate_raises_on_missing(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            with pytest.raises(RuntimeError, match="Missing required system configs"):
                store.validate_required(["key1", "key2"])

    def test_validate_lists_all_missing(self):
        store = ConfigStore("http://test:8000")
        with patch("shared.config_store.httpx.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            with pytest.raises(RuntimeError, match="key1.*key2"):
                store.validate_required(["key1", "key2"])
