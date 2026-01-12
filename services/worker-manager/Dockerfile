FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
# git is needed if we want to support git operations in the manager itself (unlikely but safe)
# curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install shared module
COPY shared ./shared
RUN pip install ./shared

# Install dependencies
COPY services/worker-manager/pyproject.toml .
RUN pip install .

# Copy source
COPY services/worker-manager/src ./src

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
