# syntax=docker/dockerfile:1.4
FROM python:3.12.13-slim

WORKDIR /app

# Install SSH client and sshpass for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install the locked service dependencies, then the pinned Ansible collections, before any
# source: a shared/ or src/ edit reuses both layers. The service's own code runs from
# PYTHONPATH=/app, so it is not pip-installed.
COPY services/infra-service/requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/requirements.lock
COPY services/infra-service/ansible/requirements.yml /app/ansible/requirements.yml
RUN ansible-galaxy collection install -r /app/ansible/requirements.yml

# Copy shared as plain files (deps are in the service requirements.lock)
COPY shared /app/shared

# Copy source code
COPY services/infra-service/src /app/src
COPY services/infra-service/ansible /app/ansible

ENV PYTHONPATH=/app

COPY services/infra-service/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "src.main"]

# Which tree this image baked shared from; read by scripts/shared_freshness.py.
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH
