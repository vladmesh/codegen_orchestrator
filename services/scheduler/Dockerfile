FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (openssh-client for health checks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy and install service dependencies
COPY services/scheduler/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/scheduler/src ./src
COPY services/api/src/models ./src/models
COPY services/api/src/clients ./src/clients
COPY shared ./shared

# Run
CMD ["python", "-m", "src.main"]
