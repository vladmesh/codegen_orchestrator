# Worker base image with Factory CLI pre-installed
# Inherits from worker-base-common

FROM worker-base-common:latest

USER root

# Install Factory CLI (Droid)
RUN curl -fsSL https://app.factory.ai/cli | sh && \
    cp /root/.local/bin/droid /usr/local/bin/ && \
    chmod +x /usr/local/bin/droid

USER worker
