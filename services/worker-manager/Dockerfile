# syntax=docker/dockerfile:1.4
FROM python:3.12.13-slim

WORKDIR /app

# Install system dependencies + Docker CLI + Compose plugin
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-26.1.4.tgz \
      | tar xz -C /usr/local/bin --strip-components=1 docker/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL https://github.com/docker/compose/releases/download/v2.27.1/docker-compose-linux-x86_64 \
       -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Create non-root user; it owns /app, and the files below are copied in as its own, so no
# recursive chown duplicates /app into another layer.
RUN useradd -m -u 1000 appuser && \
    chown appuser:appuser /app

# Install the locked service dependencies before any source: a shared/ or src/ edit
# reuses this layer. The service's own code runs from PYTHONPATH=/app.
COPY services/worker-manager/requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.lock

# Copy shared as plain files (deps are in the service requirements.lock)
COPY --chown=appuser:appuser shared ./shared

# Copy source
COPY --chown=appuser:appuser services/worker-manager/src ./src

USER appuser

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Which tree this image baked shared from; read by scripts/shared_freshness.py.
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
