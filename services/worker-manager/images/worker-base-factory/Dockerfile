# Worker base image with Factory CLI pre-installed
# Inherits from worker-base-common

ARG BASE_IMAGE=worker-base-common:latest
FROM ${BASE_IMAGE}

USER root

# Install Factory CLI (Droid)
RUN curl -fsSL https://app.factory.ai/cli | sh && \
    cp /root/.local/bin/droid /usr/local/bin/ && \
    chmod +x /usr/local/bin/droid

USER worker
