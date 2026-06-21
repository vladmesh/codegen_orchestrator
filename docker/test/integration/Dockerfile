FROM python:3.12-slim

WORKDIR /app

# System deps (git needed for scaffolded workspace fixture)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install pinned test toolchain (derived from uv.lock — same versions as the services)
COPY requirements-test.lock .
RUN pip install --no-cache-dir -r requirements-test.lock

# Copy shared and packages
COPY shared ./shared
COPY packages ./packages
COPY services ./services

# Copy tests (usually mounted, but good for caching deps)
COPY tests/integration /app/tests/integration

ENV PYTHONPATH=/app

CMD ["pytest"]
