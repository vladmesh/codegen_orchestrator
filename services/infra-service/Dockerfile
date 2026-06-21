# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

# Install SSH client and sshpass for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install service dependencies from the lock file (pinned) + Ansible collections.
# ansible-galaxy comes from ansible-core, which is pinned in requirements.lock.
COPY services/infra-service/requirements.lock .
COPY services/infra-service/ansible/requirements.yml /app/ansible/requirements.yml
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.lock && \
    ansible-galaxy collection install -r /app/ansible/requirements.yml

# Copy shared as plain files (its runtime deps are included in requirements.lock)
COPY shared /app/shared

# Copy source code
COPY services/infra-service/src /app/src
COPY services/infra-service/ansible /app/ansible

ENV PYTHONPATH=/app

COPY services/infra-service/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "src.main"]
