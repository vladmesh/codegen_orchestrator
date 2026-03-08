# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

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

# Install shared dependencies first (cached when pyproject.toml is stable)
COPY shared/pyproject.toml /tmp/shared-pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; deps=tomllib.load(open('/tmp/shared-pyproject.toml','rb'))['project']['dependencies']; print('\n'.join(deps))" \
    > /tmp/shared-requirements.txt && \
    pip install -r /tmp/shared-requirements.txt

# Install shared package code (no deps — fast)
COPY shared ./shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps ./shared

# Install service dependencies
COPY services/worker-manager/pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# Copy source
COPY services/worker-manager/src ./src

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

ENV PYTHONPATH=/app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
