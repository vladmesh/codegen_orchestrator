# QA Report

## Summary
- **Result**: failed
- **Checks**: 8 passed, 3 failed

## Checks

### 1. Health endpoint responds with 200
- **Result**: pass
- **Detail**: `curl -sf http://80.209.235.229:8012/health` returned HTTP 200 with body `{"status": "ok"}`

### 2. GET /api/weather/{city} returns 200 with JSON (path check)
- **Result**: fail
- **Detail**: `curl -v http://80.209.235.229:8012/api/weather/Moscow` returned **HTTP 404** `{"detail":"Not Found"}`. The actual working endpoint is `/weather/{city}` (no `/api` prefix). Confirmed via `GET /openapi.json` which lists path `/weather/{city}`.

### 3. GET /weather/Moscow returns JSON with required fields
- **Result**: fail
- **Detail**: `curl -sf http://80.209.235.229:8012/weather/Moscow` returned:
  ```json
  {
    "city": "Moscow",
    "temperature_celsius": -18.3,
    "humidity_percent": 65,
    "condition": "Partly cloudy",
    "cached_at": "2026-03-20T03:04:41.918996Z"
  }
  ```
  Acceptance criteria requires fields `temperature`, `humidity`, and `description`. Actual field names are `temperature_celsius`, `humidity_percent`, and `condition` — field names do not match the spec. (Data is functionally present but field names differ.)

### 4. GET /weather/Moscow returns cached data on second request within 30 minutes
- **Result**: pass
- **Detail**: Second request to `curl -sf http://80.209.235.229:8012/weather/Moscow` returned identical `cached_at: "2026-03-20T03:04:41.918996Z"` as the first request, confirming cache hit. Note: tested at the working path `/weather/Moscow`; `/api/weather/Moscow` returns 404.

### 5. GET /weather/London returns different data than Moscow
- **Result**: pass
- **Detail**: `curl -sf http://80.209.235.229:8012/weather/London` returned `{"city":"London","temperature_celsius":-4.6,"humidity_percent":35,"condition":"Snow","cached_at":"2026-03-20T03:04:42.797578Z"}` vs Moscow `{"temperature_celsius":-18.3,"humidity_percent":65,"condition":"Partly cloudy"}`. Data is clearly different. Note: t
