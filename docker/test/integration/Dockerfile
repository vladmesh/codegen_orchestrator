FROM python:3.12-slim

WORKDIR /app

# System deps (git needed for scaffolded workspace fixture)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install test dependencies
COPY docker/test/integration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and packages
COPY shared ./shared
COPY packages ./packages
COPY services ./services

# Copy tests (usually mounted, but good for caching deps)
COPY tests/integration /app/tests/integration
COPY scripts/seed_system_configs.py scripts/system_configs.yaml /app/scripts/

ENV PYTHONPATH=/app

CMD ["pytest"]
