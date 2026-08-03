# Worker base image with Factory CLI pre-installed
# Inherits from worker-base-common

# No default: the builder names the worker-base-common tag it just produced, so this
# image can never be layered on top of whatever :latest happens to be lying around.
# A build without --build-arg BASE_IMAGE fails on a blank base name.
ARG BASE_IMAGE
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
