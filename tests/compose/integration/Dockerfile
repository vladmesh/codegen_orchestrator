FROM python:3.12.13-slim

WORKDIR /app

# System deps (git needed for scaffolded workspace fixture)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# The docker CLI, at the daemon's own version: the live harness's cleanup adapter
# (`run_cleanup.docker_cli_ops`) shells out to `docker`, and
# tests/integration/backend/test_run_scoped_cleanup_cli.py runs that adapter
# against the DinD daemon this runner already points DOCKER_HOST at. The binary
# is statically linked, so it runs here as it does in its own Alpine image.
COPY --from=docker:29.7.0-cli /usr/local/bin/docker /usr/local/bin/docker

# Install test dependencies
COPY tests/compose/integration/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and packages
COPY shared ./shared
COPY packages ./packages
COPY services ./services

# Copy tests (usually mounted, but good for caching deps)
COPY tests/integration /app/tests/integration
# shared_freshness.py comes along because the worker-base fixture stamps SOURCE_HASH from
# it: the hash has one producer for the whole repository.
COPY scripts/seed_system_configs.py scripts/system_configs.yaml scripts/shared_freshness.py /app/scripts/

ENV PYTHONPATH=/app

CMD ["pytest"]

# Which tree this image baked shared from; read by scripts/shared_freshness.py.
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
