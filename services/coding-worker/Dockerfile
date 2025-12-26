# Coding Worker Container
# Simple container with Factory.ai Droid CLI

FROM ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install base tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    python3 \
    python3-pip \
    jq \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Copier, Jinja2, and Redis client
RUN pip3 install --no-cache-dir copier jinja2 redis

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli.gpg \
    && chmod go+r /usr/share/keyrings/githubcli.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install Factory.ai Droid CLI
RUN curl -fsSL https://app.factory.ai/cli | sh

# Add droid to PATH
ENV PATH="/root/.local/bin:$PATH"

# Copy scripts
COPY scripts/execute_task.sh /scripts/execute_task.sh
COPY scripts/publish_event.py /scripts/publish_event.py
RUN chmod +x /scripts/execute_task.sh

WORKDIR /workspace

CMD ["/scripts/execute_task.sh"]
