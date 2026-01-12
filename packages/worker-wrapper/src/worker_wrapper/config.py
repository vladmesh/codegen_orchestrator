from pydantic import Field
from pydantic_settings import BaseSettings


class WorkerWrapperConfig(BaseSettings):
    """Configuration for Worker Wrapper."""

    redis_url: str = Field(..., description="Redis URL connection string")
    input_stream: str = Field(..., description="Redis stream to read tasks from")
    output_stream: str = Field(..., description="Redis stream to write results to")
    consumer_group: str = Field(..., description="Consumer group name")
    consumer_name: str = Field(..., description="Consumer instance name")
    agent_type: str = Field(..., description="Agent type (claude, factory, etc.)")

    # Optional execution settings
    poll_interval_ms: int = 500
    subprocess_timeout_seconds: int = 300

    model_config = {"env_prefix": "WORKER_"}
