import httpx
import redis.asyncio as redis

from orchestrator_cli.config import Config


# Global/Singleton-like access or just factory
def get_config() -> Config:
    return Config()


def get_api_client() -> httpx.AsyncClient:
    config = get_config()
    return httpx.AsyncClient(base_url=config.api_url)


def get_redis_client() -> redis.Redis:
    config = get_config()
    return redis.from_url(config.redis_url, decode_responses=True)
