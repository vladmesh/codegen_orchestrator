# syntax=docker/dockerfile:1.4
FROM python:3.12-slim

WORKDIR /app

# Install SSH client and sshpass for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install shared dependencies first (cached when pyproject.toml is stable)
COPY shared/pyproject.toml /tmp/shared-pyproject.toml
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; deps=tomllib.load(open('/tmp/shared-pyproject.toml','rb'))['project']['dependencies']; print('\n'.join(deps))" \
    > /tmp/shared-requirements.txt && \
    pip install -r /tmp/shared-requirements.txt

# Install shared package code (no deps — fast)
COPY shared /app/shared
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-deps /app/shared

# Install service dependencies + Ansible collections
COPY services/infra-service/pyproject.toml .
COPY services/infra-service/ansible/requirements.yml /app/ansible/requirements.yml
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . && \
    ansible-galaxy collection install -r /app/ansible/requirements.yml

# Copy source code
COPY services/infra-service/src /app/src
COPY services/infra-service/ansible /app/ansible

ENV PYTHONPATH=/app

COPY services/infra-service/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "src.main"]
