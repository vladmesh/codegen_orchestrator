FROM python:3.12-slim

WORKDIR /workspace

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install uv from official image (v0.9.18)
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

# Install Python dev tools
RUN pip install --no-cache-dir \
    ruff==0.8.4 \
    pytest \
    pytest-asyncio \
    pytest-cov \
    mypy

# Default command
CMD ["bash"]
