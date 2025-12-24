FROM python:3.12-slim

WORKDIR /app

# Copy and install shared module first
COPY shared ./shared
RUN pip install --no-cache-dir ./shared

# Copy and install service dependencies
COPY services/langgraph/pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy source
COPY services/langgraph/src ./src
COPY secrets.yaml ./secrets.yaml

# Run worker
CMD ["python", "-m", "src.worker"]
