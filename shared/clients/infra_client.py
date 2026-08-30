"""HTTP health-check client."""

from __future__ import annotations


async def check_http_health(
    url: str,
    timeout: int = 5,
) -> dict:
    """Check HTTP health endpoint.

    Args:
        url: Full URL to check (e.g., http://1.2.3.4:8080/health)
        timeout: Request timeout in seconds

    Returns:
        {"healthy": True/False, "status_code": 200, "response_time_ms": 45}
    """
    import time

    import httpx

    start = time.time()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout)

        response_time_ms = int((time.time() - start) * 1000)

        return {
            "healthy": response.is_success,
            "status_code": response.status_code,
            "response_time_ms": response_time_ms,
        }

    except httpx.TimeoutException:
        return {
            "healthy": False,
            "error": "Request timed out",
            "response_time_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "healthy": False,
            "error": str(e),
            "response_time_ms": int((time.time() - start) * 1000),
        }
