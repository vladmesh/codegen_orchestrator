# QA Report

## Summary
- **Result**: passed
- **Checks**: 10 passed, 0 failed
- **Date**: 2026-03-20

## Checks

### 1. Health endpoint responds with 200
- **Result**: pass
- **Detail**: `curl -sf http://80.209.235.229:8012/health | jq .` → `{"status": "ok"}` (HTTP 200)

### 2. GET /api/weather/Moscow returns 200 with required fields
- **Result**: pass
- **Detail**: `curl -sf http://80.209.235.229:8012/api/weather/Moscow | jq .` →
  ```json
  {
    "city": "moscow",
    "temperature": -0.2,
    "description": "Heavy rain",
    "humidity": 44,
    "wind_speed": 7.8,
    "cached_at": "2026-03-20T14:39:53.775765Z"
  }
  ```
  All required fields present: temperature, description, humidity, wind_speed.

### 3. GET /api/weather/London returns 200 with weather data
- **Result**: pass
- **Detail**: `curl -sf http://80.209.235.229:8012/api/weather/London | jq .` →
  ```json
  {
    "city": "london",
    "temperature": 2.7,
    "description": "Heavy rain",
    "humidity": 72,
    "wind_speed": 21.4,
    "cached_at": "2026-03-20T14:39:55.825714Z"
  }
  ```

### 4. Subsequent GET /api/weather/Moscow within 30 minutes returns cached data (same cached_at)
- **Result**: pass
- **Detail**: Two separate requests to `/api/weather/Moscow` (approx 5 minutes apart) both returned `"cached_at": "2026-03-20T14:39:53.775765Z"` — identical timestamp confirms cached response is served.

### 5. Telegram: /weather Moscow responds with formatted weather message
- **Result**: pass
- **Detail**: Sent `/weather Moscow` via Telethon as user. Bot replied:
  ```
  Weather in Moscow

  Temperature: -0.2°C
  Description: Heavy rain
  Humidity: 44%
  Wind speed: 7.8 m/s
  ```
  Response includes temperature, description, humidity, and wind speed — all required fields.

### 6. Telegram: /weather without city parameter responds with usage instructions
- **Result**: pass
- **Detail**: Sent `/weather` (no city) via Telethon. Bot replied:
  ```
  Please provide a city name.

  Usage: /weather <city>
  Examp
