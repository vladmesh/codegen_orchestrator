FROM python:3.12-slim

WORKDIR /app

# Install Docker CLI (for spawning containers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY services/worker-spawner/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/worker-spawner/src ./src

CMD ["python", "-m", "src.main"]

