# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

# Install SSH client and sshpass for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY services/infra-service/pyproject.toml .
COPY services/infra-service/ansible/requirements.yml /app/ansible/requirements.yml
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . && \
    ansible-galaxy collection install -r /app/ansible/requirements.yml

# Copy source code
COPY services/infra-service/src /app/src
COPY services/infra-service/ansible /app/ansible
COPY shared /app/shared

ENV PYTHONPATH=/app

COPY services/infra-service/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "src.main"]
