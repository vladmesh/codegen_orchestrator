FROM python:3.12-slim

WORKDIR /app

# Install Ansible and dependencies
RUN apt-get update && apt-get install -y \
    ssh \
    sshpass \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir ansible httpx

# Create ansible directory structure to mount into
RUN mkdir -p /app/ansible

# Keep container alive
CMD ["tail", "-f", "/dev/null"]
