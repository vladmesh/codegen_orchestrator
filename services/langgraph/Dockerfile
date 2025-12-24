FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*


WORKDIR /app

# Copy and install shared module first
COPY shared ./shared
RUN pip install --no-cache-dir ./shared

# Copy and install service dependencies
COPY services/langgraph/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/langgraph/src ./src
COPY services/infrastructure ./services/infrastructure
COPY secrets.yaml ./secrets.yaml

# Run worker
CMD ["python", "-m", "src.worker"]
