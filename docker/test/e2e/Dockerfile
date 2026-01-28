FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (git for copier, curl for healthcheck, docker-cli for image building)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install test dependencies
COPY docker/test/e2e/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared and packages
COPY shared ./shared
COPY packages ./packages
COPY services ./services

# Copy tests
COPY tests/e2e /app/tests/e2e

ENV PYTHONPATH=/app

CMD ["pytest", "tests/e2e/", "-v", "-m", "e2e"]
