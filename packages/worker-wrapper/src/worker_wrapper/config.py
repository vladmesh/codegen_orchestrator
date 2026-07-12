from pydantic import Field
from pydantic_settings import BaseSettings

from shared.contracts.vocab import AgentType


class WorkerWrapperConfig(BaseSettings):
    """Configuration for Worker Wrapper."""

    redis_url: str = Field(..., description="Redis URL connection string")
    input_stream: str = Field(..., description="Redis stream to read tasks from")
    output_stream: str = Field(..., description="Redis stream to write results to")
    consumer_group: str = Field(..., description="Consumer group name")
    consumer_name: str = Field(..., description="Consumer instance name")
    agent_type: AgentType = Field(..., description="Which coding agent runs in this worker")

    # Optional execution settings
    poll_interval_ms: int = 500
    subprocess_timeout_seconds: int = 300
    http_server_port: int = 9090

    model_config = {"env_prefix": "WORKER_"}
