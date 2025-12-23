FROM python:3.12-slim

WORKDIR /workspace

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dev tools
RUN pip install --no-cache-dir \
    ruff \
    pytest \
    pytest-asyncio \
    pytest-cov \
    mypy

# Default command
CMD ["bash"]
