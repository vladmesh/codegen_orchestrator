"""Unit tests for analytics aggregator — compute functions."""

import os

# Set required env vars before importing the module (module-level config validation)
os.environ.setdefault("API_BASE_URL", "http://test:8000")
os.environ.setdefault("LOKI_URL", "http://test:3100")

from src.tasks.analytics_aggregator import compute_daily_rollup, compute_hourly_metrics


def _make_request_log(
    path="/api/health",
    status_code=200,
    duration_ms=10.0,
    user_id="tg:1",
    level="info",
):
    return {
        "event": "request",
        "method": "GET",
        "path": path,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "user_id": user_id,
        "level": level,
    }


class TestComputeHourlyMetrics:
    def test_basic_request_counting(self):
        logs = [_make_request_log(), _make_request_log(), _make_request_log()]
        result = compute_hourly_metrics(logs, set())
        assert result["total_requests"] == 3

    def test_error_counting_5xx(self):
        logs = [
            _make_request_log(status_code=200),
            _make_request_log(status_code=500),
            _make_request_log(status_code=503),
        ]
        result = compute_hourly_metrics(logs, set())
        assert result["error_count"] == 2

    def test_error_counting_level_error(self):
        logs = [_make_request_log(level="error", status_code=200)]
        result = compute_hourly_metrics(logs, set())
        assert result["error_count"] == 1

    def test_unique_users(self):
        logs = [
            _make_request_log(user_id="tg:1"),
            _make_request_log(user_id="tg:2"),
            _make_request_log(user_id="tg:1"),  # duplicate
        ]
        result = compute_hourly_metrics(logs, set())
        assert result["unique_users"] == 2

    def test_new_users_vs_known(self):
        # Pre-hash user tg:1
        from src.tasks.analytics_aggregator import _hash_user_id

        known = {_hash_user_id("tg:1")}
        logs = [
            _make_request_log(user_id="tg:1"),
            _make_request_log(user_id="tg:2"),
        ]
        result = compute_hourly_metrics(logs, known)
        assert result["unique_users"] == 2
        assert result["new_users"] == 1  # only tg:2 is new

    def test_percentiles(self):
        logs = [_make_request_log(duration_ms=d) for d in [10, 20, 30, 40, 50]]
        result = compute_hourly_metrics(logs, set())
        assert result["p50_ms"] is not None
        assert result["p95_ms"] is not None

    def test_top_endpoints(self):
        logs = [
            _make_request_log(path="/start"),
            _make_request_log(path="/start"),
            _make_request_log(path="/help"),
        ]
        result = compute_hourly_metrics(logs, set())
        assert result["top_endpoints"][0]["path"] == "/start"
        assert result["top_endpoints"][0]["count"] == 2

    def test_non_request_events_ignored(self):
        logs = [
            {"event": "startup", "service": "backend"},
            _make_request_log(),
        ]
        result = compute_hourly_metrics(logs, set())
        assert result["total_requests"] == 1

    def test_empty_logs(self):
        result = compute_hourly_metrics([], set())
        assert result["total_requests"] == 0
        assert result["unique_users"] == 0
        assert result["p50_ms"] is None

    def test_seen_users_output(self):
        logs = [_make_request_log(user_id="tg:42")]
        result = compute_hourly_metrics(logs, set())
        assert len(result["seen_users"]) == 1
        assert "user_id_hash" in result["seen_users"][0]


class TestComputeDailyRollup:
    def test_sums_requests_and_errors(self):
        hourly = [
            {
                "total_requests": 100,
                "error_count": 5,
                "unique_users": 10,
                "new_users": 3,
                "p95_ms": 50.0,
            },
            {
                "total_requests": 200,
                "error_count": 10,
                "unique_users": 20,
                "new_users": 7,
                "p95_ms": 80.0,
            },
        ]
        result = compute_daily_rollup(hourly, [])
        assert result["total_requests"] == 300
        assert result["error_count"] == 15
        assert result["new_users"] == 10

    def test_worst_of_p95(self):
        hourly = [
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 5,
                "new_users": 1,
                "p95_ms": 30.0,
            },
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 5,
                "new_users": 1,
                "p95_ms": 90.0,
            },
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 5,
                "new_users": 1,
                "p95_ms": 60.0,
            },
        ]
        result = compute_daily_rollup(hourly, [])
        assert result["p95_ms"] == 90.0

    def test_error_rate(self):
        hourly = [
            {
                "total_requests": 100,
                "error_count": 10,
                "unique_users": 5,
                "new_users": 1,
                "p95_ms": 50.0,
            },
        ]
        result = compute_daily_rollup(hourly, [])
        assert result["error_rate"] == 0.1

    def test_dau_is_max_hourly(self):
        hourly = [
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 5,
                "new_users": 1,
                "p95_ms": 50.0,
            },
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 15,
                "new_users": 1,
                "p95_ms": 50.0,
            },
        ]
        result = compute_daily_rollup(hourly, [])
        assert result["dau"] == 15

    def test_returning_users(self):
        hourly = [
            {
                "total_requests": 100,
                "error_count": 0,
                "unique_users": 10,
                "new_users": 3,
                "p95_ms": 50.0,
            },
        ]
        known_users = [
            {"user_id_hash": f"hash{i}", "first_seen": "2026-01-01", "last_seen": "2026-03-20"}
            for i in range(10)
        ]
        result = compute_daily_rollup(hourly, known_users)
        # 10 known - 3 new = 7 returning
        assert result["returning_users"] == 7

    def test_empty_hourly(self):
        result = compute_daily_rollup([], [])
        assert result["total_requests"] == 0
        assert result["error_rate"] == 0.0
        assert result["dau"] == 0
