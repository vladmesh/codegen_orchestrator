# Worker base image with Factory CLI pre-installed
# Inherits from worker-base-common

ARG BASE_IMAGE=worker-base-common:latest
FROM ${BASE_IMAGE}

USER root

# Install Factory CLI (Droid)
RUN curl -fsSL https://app.factory.ai/cli | sh && \
    cp /root/.local/bin/droid /usr/local/bin/ && \
    chmod +x /usr/local/bin/droid

# Own label, not inherited from the base: derived images must carry the hash they were built with
ARG SOURCE_HASH=unknown
LABEL org.codegen.worker_source_hash=$SOURCE_HASH

USER worker
