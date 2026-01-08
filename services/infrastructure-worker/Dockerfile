FROM python:3.12-slim

WORKDIR /app

# Install SSH client and sshpass for Ansible
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY services/infrastructure-worker/pyproject.toml .
COPY services/infrastructure-worker/ansible/requirements.yml /app/ansible/requirements.yml
RUN pip install --no-cache-dir . && \
    ansible-galaxy collection install -r /app/ansible/requirements.yml

# Copy source code
COPY services/infrastructure-worker/src /app/src
COPY services/infrastructure-worker/ansible /app/ansible
COPY shared /app/shared

ENV PYTHONPATH=/app

CMD ["python", "-m", "src.main"]
