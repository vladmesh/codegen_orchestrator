"""Mock Anthropic API for E2E testing."""

from .responses import get_response_for_prompt
from .server import app

__all__ = ["app", "get_response_for_prompt"]
